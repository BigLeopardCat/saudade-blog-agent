"""Bounded model/tool loop with on-demand domains and terminal tool outcomes.

This session owns tool discovery and result budgets. Conversation persistence and
legacy handoff remain in HybridArm; the model still chooses each tool and reply.
"""

import json
import time

from langchain_core.messages import ToolMessage
from langchain_core.tools import ToolException

from agent.article_evidence import ArticleEvidence
from agent.authz import SCOPE_ADMIN_CONSOLE, SCOPE_READ_OWN, TOOL_SCOPE, WRITE_SCOPES, check
from agent.domains import browser
from agent.llm_usage import usage_fields
from agent.runtime import memory
from agent.runtime.read_tools import scoped_reads_enabled
from agent.runtime.results import TurnResult
from agent.runtime.turns import check_cancelled
from tools.base import unavailable
from utils import trace as trace_mod
from utils.trace import record

MAX_PROMPT_CHARS = 24000
_MAX_TOOL_CALLS = 4
_MAX_TOOL_ROUNDS = 4
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
    "devices": "读取当前账号可访问的设备，包含共享演示设备；不下发指令。",
    "public_catalog": "分页读取公开文章清单、置顶文章、分类和分层标签及公开文章计数；不读取正文。",
    "own_activity": "读取本人河灯留言的状态、理由与分页内容，以及当前会话的真实聊天文本。"
                    "不读取其他账号数据或其他会话，不加载图片，只读。",
    "admin_catalog": "分页读取后台文章清单（含草稿、私密文章的元数据）与账号名录。"
                     "可按分类、关键词、账号名或编号筛选；可准备文章状态、置顶或标签增删替换确认；标签缺失时可同卡创建并挂载，仅管理员可用。",
    "admin_work_queue": "分页读取额度重置申请、当前管理员在后台首页记录的个人待办和日程。"
                        "仅管理员可用；可准备个人待办及额度审批或重置确认，准备不写入。",
    "admin_moderation": "读取河灯集（河灯留言）与文章评论的审核计数、分页记录和指定编号内容。"
                        "仅管理员可用；可准备指定留言或评论的通过、驳回、删除确认，准备不写入。",
    "admin_reports": "Load administrator-only server health, user statistics and article traffic "
                     "reports. Read-only; does not change site data or service state.",
    "site": "读取博客作者信息、社交链接和站点功能结构；身份获授权时还可跳转页面、设置主题和特效。",
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
        self.model = model
        self.config = config
        self.principal = ((config or {}).get("configurable") or {}).get("principal")
        self.tools = dict(tools)
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
        self.llm = self._bind_menu()

    def _bind_menu(self):
        menu = [*self.tools.values(), *(_loader(domain) for domain in self.domains
                                       if domain not in self.loaded)]
        return self.model.bind_tools([*menu, _DELEGATE], tool_choice="auto",
                                     parallel_tool_calls=False)

    def _handoff(self, reason, **data):
        if self.receipts:
            record("hybrid", "handoff_stopped", reason=reason, **data)
            return self._result("页面指令已发送；其余请求尚未完成，请继续指定要处理的事项。")
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
        if round_index >= _MAX_TOOL_ROUNDS:
            return "call_budget"
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
        tool_count = sum(call.get("name") in self.tools for call in calls)
        if self.calls_used + tool_count > _MAX_TOOL_CALLS:
            return "call_budget"
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
        if scope in WRITE_SCOPES:
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
            prompt.append(ToolMessage(content=f"{domain} 工具已加载。",
                                      tool_call_id=call["id"]))
            record("hybrid", "load_domain", domain=domain)
            self.llm = self._bind_menu()
            return True
        if name not in self.tools:
            return self._tool_error(call, prompt, "unknown_tool")
        if self.calls_used >= _MAX_TOOL_CALLS:
            self._budget_stop("call_budget", tool_calls=self.calls_used)
            return False
        if self.result_chars >= _MAX_TOTAL_RESULT_CHARS:
            self._budget_stop("result_budget")
            return False
        args = call["args"]
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
            if not browser.authorized(self.config, name):
                raise RuntimeError("Only authorized browser tools may issue page receipts")
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
        self.calls_used += 1
        record("hybrid", "call", name=name, args=args, round=round_index,
               kind=kind, result=trace_mod.tool_result_text(body, name))
        record("hybrid", "tool_result", name=name, result_chars=len(body))
        return True

    def _restore_domains(self, prompt):
        observed = {message.name for message in prompt
                    if isinstance(message, ToolMessage) and message.status != "error"}
        restored = [domain for domain, tools in self.domains.items()
                    if domain not in self.loaded and observed.intersection(tools)]
        for domain in restored:
            self.tools.update(self.domains[domain])
            self.loaded.add(domain)
        if restored:
            self.llm = self._bind_menu()
            record("hybrid", "restore_domains", domains=restored)

    def run(self, prompt):
        self._restore_domains(prompt)
        for round_index in range(_MAX_TOOL_ROUNDS + 1):
            check_cancelled(self.config)
            prompt_chars = sum(len(str(message.content)) for message in prompt)
            if prompt_chars > MAX_PROMPT_CHARS:
                return self._budget_stop("prompt_budget", prompt_chars=prompt_chars)
            started = time.monotonic()
            record("hybrid", "llm_start", round=round_index, engine="native",
                   prompt_chars=prompt_chars)
            response = self.llm.invoke(prompt)
            record("hybrid", "llm_done", round=round_index, engine="native",
                   duration_s=round(time.monotonic() - started, 2),
                   prompt_chars=prompt_chars, **usage_fields(response))
            check_cancelled(self.config)
            calls = getattr(response, "tool_calls", None) or []
            if not calls:
                reply = str(getattr(response, "content", "") or "").strip()
                from tools.base import SITE_BASE
                issue = self.article_evidence.issue(reply, SITE_BASE)
                if issue:
                    return self._handoff(issue, tool_calls=self.calls_used)
                if self.article_evidence.searched and not self.article_evidence.documents:
                    if self.article_evidence.failed and not self.article_evidence.found:
                        return self._result("文章检索暂时不可用，当前无法核实文章内容。")
                    if self.article_evidence.empty and not self.article_evidence.found:
                        return self._result("暂未从公开文章中检索到相关资料。")
                reply = self.article_evidence.sources(reply, SITE_BASE)
                return self._result(reply) if reply else self._handoff(
                    "empty", tool_calls=self.calls_used)
            reason = self._accept_calls(calls, round_index)
            if reason:
                if reason == "call_budget":
                    return self._budget_stop(reason, tool_calls=self.calls_used)
                return self._handoff(reason, tool_calls=self.calls_used)
            prompt.append(response)
            for call in calls:
                check_cancelled(self.config)
                if not self._append_result(call, prompt, round_index):
                    return self.outcome
                if self.outcome is not None:
                    return self.outcome
        return self._handoff("empty", tool_calls=self.calls_used)
