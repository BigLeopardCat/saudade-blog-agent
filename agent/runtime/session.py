"""Bounded model/tool loop with on-demand domains and terminal tool outcomes.

This session owns tool discovery and result budgets. Conversation persistence and
legacy handoff remain in HybridArm; the model still chooses each tool and reply.
"""

import json
import time

from langchain_core.messages import AIMessage, AIMessageChunk, ToolMessage
from langchain_core.tools import ToolException

from agent.article_evidence import ArticleEvidence
from agent.authz import SCOPE_ADMIN_CONSOLE, SCOPE_READ_OWN, TOOL_SCOPE, WRITE_SCOPES, check
from agent.domains import browser, device_actions
from agent.llm_usage import usage_fields
from agent.runtime import memory
from agent.runtime.budget import (
    NATIVE_OUTPUT_TOKENS,
    BudgetExceeded,
    RequestBudget,
    current_budget,
    native_call,
    native_tool,
)
from agent.runtime.catalog import initial_tools
from agent.runtime.control import model_options
from agent.runtime.progress import emit as emit_progress
from agent.runtime.read_tools import scoped_reads_enabled
from agent.runtime.results import TurnResult
from agent.runtime.turns import check_cancelled
from config.settings import settings
from tools.base import unavailable
from utils import trace as trace_mod
from utils.trace import record

MAX_PROMPT_CHARS = 24000
_MAX_RESULT_CHARS = 8000
_MAX_TOTAL_RESULT_CHARS = 16000

_DELEGATE = {
    "type": "function",
    "function": {
        "name": "delegate_to_site_agent",
        "description": (
            "处理当前按需工具领域未覆盖的站内任务。"
            "这类请求需要真实数据或执行回执时调用；普通闲聊和一般知识问答直接回复。"
        ),
        "parameters": {"type": "object", "properties": {},
                       "additionalProperties": False},
    },
}
_DOMAIN_DESCRIPTIONS = {
    "public_activity": "分页读取公开公告、已通过审核的河灯留言和说说；"
                       "可按编号读取内容，不含待审或驳回记录。",
    "utilities": "读取当前时间或指定城市的实时天气；天气附来源和观测时间。",
    "devices": "读取当前账号可访问的设备（含共享演示设备），或按请求显示OLED文字；"
               "仅已授权身份可下发，区分下发与设备确认，不管理物联网平台。",
    "public_catalog": "分页读取公开文章清单、置顶文章、分类和分层标签及公开文章计数；不读取正文。",
    "own_activity": "读取本人河灯留言的状态、理由与分页内容，以及当前会话的真实聊天文本。"
                    "不读取其他账号数据或其他会话，不加载图片，只读。",
    "admin_catalog": "分页读取后台文章清单（含草稿、私密文章的元数据）与账号名录。"
                     "可按分类、关键词、账号名或编号筛选；可准备文章状态、置顶或标签增删替换确认；标签缺失时可同卡创建并挂载，仅管理员可用。",
    "admin_notices": "给单个后台账号发送个人通知：按编号或完整账号名准备确认，"
                     "核对收件人及完整标题正文；准备不写入，仅管理员可用。",
    "admin_tags": "管理标签字典：准备新建、改名、改色、换父级、一级二级互转和删除确认；"
                  "核对层级身份、子标签与文章引用。"
                  "给文章挂标签请用后台文章领域，仅管理员可用。",
    "admin_account_actions": "Prepare account freeze, unfreeze, role change, mute or unmute "
                             "by exact username or ID. Show before/after state, deadlines, "
                             "session revocation and notification effects; administrator only, "
                             "preparation never writes.",
    "admin_announcements": "管理站内公告：准备发布、修改、删除确认，"
                           "展示完整正文及关联个人通知范围；"
                           "准备不写入，仅管理员可用。",
    "admin_categories": "管理文章分类：按唯一编号或完整名字准备新建、修改、删除确认，"
                        "核对字段与关联文章和编辑草稿范围；准备不写入，仅管理员可用。",
    "admin_work_queue": "分页读取额度重置申请、当前管理员在后台首页记录的个人待办和日程。"
                        "仅管理员可用；可准备个人待办及额度审批或重置确认，准备不写入。",
    "admin_moderation": "读取河灯集（河灯留言）与文章评论的审核计数、分页记录和指定编号内容。"
                        "仅管理员可用；可准备指定留言或评论的通过、驳回、删除确认，准备不写入。",
    "admin_reports": "Load administrator-only server health, user statistics and article traffic "
                     "reports. Read-only; does not change site data or service state.",
    "site": "按用户请求打开站内页面（包括选定的推荐文章）、设置主题和特效；"
            "也可读取作者信息、社交链接和站点功能结构。",
    "favorites": "按需加载本人收藏读取与可用的收藏变更确认工具；确认准备不写入。",
    "notifications": "按需加载本人通知读取与可用的已读确认工具；查看不代表标记已读。",
    "messages": "按需加载本人信件摘要、单封正文与可用的收件箱已读确认工具；查看不标记已读。",
    "pending": "用户要求放弃当前待确认操作时加载取消工具。修改目标则加载对应领域工具。",
}


def _loader(domain: str) -> dict:
    return {"type": "function", "function": {
        "name": f"load_{domain}_info_tools",
        "description": _DOMAIN_DESCRIPTIONS[domain],
        "parameters": {"type": "object", "properties": {},
                       "additionalProperties": False},
    }}


class ToolSession:
    def __init__(self, model, config, tools, domains, outcome_adapter=None):
        self.budget = current_budget() or RequestBudget()
        self.output_limit = getattr(model, "max_tokens", None) or NATIVE_OUTPUT_TOKENS
        self.model = model
        self.config = config
        self.principal = ((config or {}).get("configurable") or {}).get("principal")
        self.tools = initial_tools(tools, domains)
        self.initial_names = set(self.tools)
        self.domains = domains
        self.loaders = {f"load_{domain}_info_tools": domain for domain in domains}
        self.loaded: set[str] = set()
        self.calls_used = 0
        self.result_chars = 0
        self.outcome: TurnResult | None = None
        self.outcome_adapter = outcome_adapter
        self.observations = []
        self.receipts = []
        self.article_evidence = ArticleEvidence()
        self.used_stream_api = False
        self.llm = self._bind_menu()

    def _bind_menu(self):
        menu = [*self.tools.values(), *(_loader(domain) for domain in self.domains
                                       if domain not in self.loaded)]
        self.menu = [*menu, _DELEGATE]
        bound = self.model.bind_tools(self.menu, tool_choice="auto", parallel_tool_calls=False)
        wire = getattr(bound, "kwargs", {})
        if isinstance(wire, dict) and isinstance(wire.get("tools"), list):
            self.menu = wire["tools"]
        return bound

    def _handoff(self, reason, **data):
        if self.receipts:
            record("hybrid", "handoff_stopped", reason=reason, **data)
            return self._result("工具操作结果已保留；其余请求尚未完成，请继续指定要处理的事项。")
        if self.calls_used:
            record("hybrid", "handoff_stopped", reason=reason, **data)
            if reason in ("article_body_missing", "unread_article_reference"):
                return self._result("暂时无法核实文章正文，不能据此回答文章内容。"
                                    "请重新指定要阅读的文章。")
            return self._result("本轮查询已停止，暂时没能完成请求。请明确下一步要处理的事项。")
        record("hybrid", "legacy_handoff", reason=reason, **data)
        return None

    def _result(self, reply):
        return self._with_receipts(TurnResult(reply))

    def _with_receipts(self, result):
        if not self.receipts:
            return result
        execution = result.execution or {}
        return TurnResult(result.reply, {**execution, "receipts": [
            *self.receipts, *(execution.get("receipts") or [])]})

    def _budget_stop(self, reason, **data):
        record("hybrid", "budget_stop", reason=reason, **data)
        self.outcome = self._result("本轮操作已达到调用或上下文上限，已停止继续处理。"
                                    "请缩小请求范围后再试。")
        return self.outcome

    def _accept_calls(self, calls, round_index):
        for call in calls:
            if (not isinstance(call, dict) or not isinstance(call.get("name"), str)
                    or not call.get("name") or not isinstance(call.get("id"), str)
                    or not call.get("id") or not isinstance(call.get("args"), dict)):
                return "invalid_tool_call"
        if any(call.get("name") == "delegate_to_site_agent" for call in calls):
            return "model_delegate"
        if len(calls) > 1 and any(getattr(self.tools.get(call.get("name")), "return_direct", False)
                                  for call in calls):
            return "parallel_terminal_calls"
        return None

    def _tool_error(self, call, prompt, reason):
        available = sorted({*self.tools, "delegate_to_site_agent", *(name for name, domain
                            in self.loaders.items() if domain not in self.loaded)})
        body = json.dumps({"error": reason, "available_tools": available})
        prompt.append(ToolMessage(content=body,
                                  tool_call_id=call["id"], status="error"))
        record("hybrid", "tool_error", name=call["name"], reason=reason)
        return True

    def _invoke_tool(self, name, args, call=None):
        scope = TOOL_SCOPE.get(name)
        if scope in WRITE_SCOPES and not device_actions.authorized(self.config, name):
            record("hybrid", "tool_denied", name=name)
            return None
        if scope in (SCOPE_READ_OWN, SCOPE_ADMIN_CONSOLE):
            uid = ((self.config or {}).get("configurable") or {}).get("user_id")
            if (not scoped_reads_enabled(self.principal, scope, uid)
                    or not check(self.principal, name).allowed):
                record("hybrid", "tool_denied", name=name)
                return None
        try:
            if getattr(self.tools[name], "response_format", None) == "content_and_artifact":
                return self.tools[name].invoke({**call, "type": "tool_call"}, config=self.config)
            return self.tools[name].invoke(args, config=self.config)
        except (ToolException, ValueError) as exc:
            record("hybrid", "tool_failed", name=name, error=type(exc).__name__)
            return unavailable(f"{name} 调用失败（{type(exc).__name__}），请如实说明。")

    def _append_result(self, call, prompt, round_index):
        name = call["name"]
        record("hybrid", "tool_requested", name=name)
        if name in self.loaders:
            domain = self.loaders[name]
            if domain in self.loaded:
                return self._tool_error(call, prompt, "domain_already_loaded")
            self.tools.update(self.domains[domain])
            self.loaded.add(domain)
            emit_progress(self.config, "正在加载可用工具…", key=f"domain:{domain}")
            prompt.append(ToolMessage(content=f"{domain} 工具已加载。",
                                      tool_call_id=call["id"]))
            record("hybrid", "load_domain", domain=domain)
            self.llm = self._bind_menu()
            return True
        if name not in self.tools:
            return self._tool_error(call, prompt, "unknown_tool")
        if self.result_chars >= _MAX_TOTAL_RESULT_CHARS:
            self._budget_stop("result_budget")
            return False
        args = call["args"]
        self.calls_used += 1
        emit_progress(self.config, "正在处理工具请求…", key=f"tool:{name}")
        with native_tool(self.budget):
            body = self._invoke_tool(name, args, call)
        if isinstance(body, TurnResult):
            # Confirmation tokens stay in the terminal outcome, never in the model prompt or trace.
            adapted = self.outcome_adapter(body) if self.outcome_adapter else body
            self.outcome = self._with_receipts(adapted)
            execution = self.outcome.execution or {}
            kind = ("prepared" if execution.get("pending_confirm") else
                    "noop" if execution.get("noop_note") else
                    "cancelled" if execution.get("pending_event") else "terminal")
            record("hybrid", "call", name=name, round=round_index, kind=kind)
            return True
        if body is None:
            prompt.append(ToolMessage(content="无权读取此数据。", tool_call_id=call["id"]))
            return True
        message = body if isinstance(body, ToolMessage) else None
        artifact = message.artifact if message and isinstance(message.artifact, dict) else {}
        if artifact.get("receipts"):
            if not (browser.authorized(self.config, name)
                    or device_actions.authorized(self.config, name)):
                raise RuntimeError("Only authorized browser tools or device tools "
                                   "may issue receipts")
            if name == device_actions.NAME and any("cmd" in row for row in artifact["receipts"]):
                raise RuntimeError("Device tools cannot issue browser commands")
            self.receipts.extend(artifact["receipts"])
        kind = artifact.get("kind", "unknown") if message else getattr(body, "kind", "unknown")
        if message and message.status == "error":
            kind = "error"
        self.observations.extend(memory.observation(call, body))
        evidence_value = body
        body = str(message.content) if message else str(body)
        remaining = _MAX_TOTAL_RESULT_CHARS - self.result_chars
        limit = min(_MAX_RESULT_CHARS, remaining)
        if name == "get_article_detail":
            from rag.reading import render_article_result
            from tools.result import ToolResult
            body, visible = render_article_result(evidence_value, limit)
            evidence_value = ToolResult(body, kind=kind, data=visible)
        elif len(body) > limit:
            marker = "\n[结果截断，请缩小检索范围]"
            body = body[:max(0, limit - len(marker))] + marker
        self.article_evidence.observe(name, args, evidence_value)
        prompt.append(message.model_copy(update={"content": body}) if message else
                      ToolMessage(content=body, tool_call_id=call["id"]))
        self.result_chars += len(body)
        record("hybrid", "call", name=name, args=args, round=round_index,
               kind=kind, result=trace_mod.tool_result_text(body, name))
        record("hybrid", "tool_result", name=name, result_chars=len(body))
        return True

    def _restore_domains(self, prompt):
        observed = {message.name for message in prompt
                    if isinstance(message, ToolMessage) and message.status != "error"}
        observed.difference_update(self.initial_names)
        restored = [domain for domain, tools in self.domains.items()
                    if domain not in self.loaded and observed.intersection(tools)]
        for domain in restored:
            self.tools.update(self.domains[domain])
            self.loaded.add(domain)
        if restored:
            self.llm = self._bind_menu()
            record("hybrid", "restore_domains", domains=restored)

    @staticmethod
    def _message_from_chunk(chunk):
        """Turn the completed public chunk aggregate into a replayable AIMessage."""
        if isinstance(chunk, AIMessage):
            return chunk
        return AIMessage(
            content=chunk.content,
            additional_kwargs=dict(chunk.additional_kwargs or {}),
            response_metadata=dict(chunk.response_metadata or {}),
            name=chunk.name,
            id=chunk.id,
            tool_calls=list(chunk.tool_calls or []),
            invalid_tool_calls=list(chunk.invalid_tool_calls or []),
            usage_metadata=chunk.usage_metadata,
        )

    def _model_stream(self, prompt):
        """Yield provider chunks and return one complete, replayable response.

        The budget context surrounds iterator consumption, so a request reserves
        and settles exactly once even when usage arrives only on the final chunk.
        The iterator is closed on cancellation and transport errors.
        """
        iterator = None
        aggregate = None
        started = time.monotonic()
        failed = False
        emit_progress(self.config, "正在处理请求…", key=f"model:{self._round_index}")
        record("hybrid", "llm_start", round=self._round_index, engine="native",
               prompt_chars=sum(len(str(message.content)) for message in prompt),
               streaming=True)
        try:
            with native_call(self.budget, prompt, self.menu,
                             output_limit=self.output_limit) as ticket:
                if not callable(getattr(self.llm, "stream", None)):
                    check_cancelled(self.config)
                    response = self.llm.invoke(prompt, **model_options(settings.llm_timeout))
                    aggregate = response
                    yield AIMessageChunk(content=str(getattr(response, "content", "") or ""))
                else:
                    self.used_stream_api = True
                    iterator = self.llm.stream(
                        prompt, **model_options(settings.llm_timeout))
                    for chunk in iterator:
                        check_cancelled(self.config)
                        if isinstance(chunk, AIMessage) and not isinstance(chunk, AIMessageChunk):
                            chunk = AIMessageChunk(
                                content=chunk.content,
                                additional_kwargs=chunk.additional_kwargs,
                                response_metadata=chunk.response_metadata,
                                name=chunk.name, id=chunk.id,
                                tool_calls=chunk.tool_calls,
                                invalid_tool_calls=chunk.invalid_tool_calls,
                                usage_metadata=chunk.usage_metadata,
                            )
                        if not isinstance(chunk, AIMessageChunk):
                            continue
                        aggregate = chunk if aggregate is None else aggregate + chunk
                        yield chunk
                if aggregate is None:
                    aggregate = AIMessage(content="")
                response = self._message_from_chunk(aggregate)
                self.budget.settle(ticket, response)
            return response
        except BaseException:
            failed = True
            raise
        finally:
            close = getattr(iterator, "close", None)
            if callable(close):
                try:
                    close()
                except Exception as exc:  # noqa: BLE001 - closing is best effort
                    record("hybrid", "llm_close_error", error=type(exc).__name__)
            record("hybrid", "llm_error" if failed else "llm_done",
                   round=self._round_index, engine="native",
                   duration_s=round(time.monotonic() - started, 2),
                   streaming=True, **usage_fields(aggregate))

    def _stream_terminal(self, result, visible_parts, fallback_text=None,
                         current_text=""):
        """Append any deterministic terminal text and return persisted output."""
        displayed = "".join(visible_parts)
        reply = str(result.reply or "")
        if fallback_text is not None and reply == fallback_text:
            missing = reply
        elif current_text and reply.startswith(current_text) and displayed.endswith(current_text):
            missing = reply[len(current_text):]
        elif displayed and reply.startswith(displayed):
            missing = reply[len(displayed):]
        elif displayed and reply and displayed.endswith(reply):
            missing = ""
        elif displayed and reply:
            missing = ("\n" if not displayed.endswith("\n") else "") + reply
        else:
            missing = reply
        if missing:
            visible_parts.append(missing)
            yield AIMessageChunk(content=missing)
        return TurnResult("".join(visible_parts), result.execution)

    def _handle_response(self, response, buffered_chunks, visible_parts):
        """Validate one completed response and either finish or return calls."""
        if getattr(response, "invalid_tool_calls", None):
            record("hybrid", "invalid_tool_call",
                   count=len(response.invalid_tool_calls))
            result = self._result("本轮工具请求未完整，未执行操作。")
            result = yield from self._stream_terminal(result, visible_parts)
            return None, result
        calls = getattr(response, "tool_calls", None) or []
        if not calls:
            reply = str(getattr(response, "content", "") or "").strip()
            from tools.base import SITE_BASE
            issue = self.article_evidence.issue(reply, SITE_BASE)
            if issue:
                result = self._handoff(issue, tool_calls=self.calls_used)
                if result is None:
                    result = self._result("".join(visible_parts)) if visible_parts else None
                if result is None:
                    return None, None
                result = yield from self._stream_terminal(
                    result, visible_parts, current_text=reply)
                return None, result
            if self.article_evidence.searched and not self.article_evidence.documents:
                if self.article_evidence.failed and not self.article_evidence.found:
                    result = self._result("文章检索暂时不可用，当前无法核实文章内容。")
                elif self.article_evidence.empty and not self.article_evidence.found:
                    result = self._result("暂未从公开文章中检索到相关资料。")
                else:
                    result = self._result(reply)
                result = yield from self._stream_terminal(
                    result, visible_parts, current_text=reply)
                return None, result
            for chunk in buffered_chunks:
                visible_parts.append(str(chunk.content))
                yield chunk
            raw_reply = reply
            reply = self.article_evidence.sources(raw_reply, SITE_BASE)
            result = self._result(reply) if reply else self._handoff(
                "empty", tool_calls=self.calls_used)
            if result is None:
                return None, None
            result = yield from self._stream_terminal(
                result, visible_parts, current_text=raw_reply)
            return None, result
        reason = self._accept_calls(calls, self._round_index)
        if reason:
            result = self._handoff(reason, tool_calls=self.calls_used)
            if result is None:
                return None, None
            result = yield from self._stream_terminal(result, visible_parts)
            return None, result
        try:
            self.budget.attempt_tools(len(calls))
        except BudgetExceeded as exc:
            result = self._budget_stop(exc.reason, tool_calls=self.calls_used)
            result = yield from self._stream_terminal(result, visible_parts)
            return None, result
        for chunk in buffered_chunks:
            visible_parts.append(str(chunk.content))
            yield chunk
        return calls, None

    def stream(self, prompt):
        """Run native rounds while yielding model text chunks as they arrive."""
        self._restore_domains(prompt)
        visible_parts = []
        self._round_index = 0
        while True:
            check_cancelled(self.config)
            prompt_chars = sum(len(str(message.content)) for message in prompt)
            if prompt_chars > MAX_PROMPT_CHARS:
                result = self._budget_stop("prompt_budget", prompt_chars=prompt_chars)
                return (yield from self._stream_terminal(result, visible_parts))
            try:
                model_iterator = self._model_stream(prompt)
                buffered_chunks = []
                stream_visible = not self.article_evidence.searched
                while True:
                    try:
                        chunk = next(model_iterator)
                    except StopIteration as stop:
                        response = stop.value
                        break
                    if isinstance(chunk, AIMessageChunk) and chunk.content:
                        if stream_visible:
                            visible_parts.append(str(chunk.content))
                            yield chunk
                        else:
                            buffered_chunks.append(chunk)
            except BudgetExceeded as exc:
                result = self._budget_stop(exc.reason, tool_calls=self.calls_used)
                return (yield from self._stream_terminal(result, visible_parts))
            check_cancelled(self.config)
            calls, result = yield from self._handle_response(
                response, buffered_chunks, visible_parts)
            if result is not None:
                return result
            if calls is None:
                return None
            prompt.append(response)
            for call in calls:
                check_cancelled(self.config)
                if not self._append_result(call, prompt, self._round_index):
                    return self.outcome
                if self.outcome is not None:
                    return (yield from self._stream_terminal(self.outcome, visible_parts))
            self._round_index += 1

    def run(self, prompt):
        runner = self.stream(prompt)
        while True:
            try:
                next(runner)
            except StopIteration as stop:
                return stop.value
