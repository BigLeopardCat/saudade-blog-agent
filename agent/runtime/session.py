"""Application context and result policy for the standard LangChain Agent.

The framework schedules model and tool rounds. The application owns authorized
capabilities, evidence, budgets and the existing signed confirmation protocol.
"""

import json

from langchain.agents import create_agent
from langchain_core.messages import AIMessageChunk, SystemMessage, ToolMessage
from langchain_core.tools import ToolException
from langchain_core.utils.function_calling import convert_to_openai_tool
from langgraph.checkpoint.memory import InMemorySaver

from agent.article_evidence import ArticleEvidence
from agent.authz import SCOPE_ADMIN_CONSOLE, SCOPE_READ_OWN, TOOL_SCOPE, WRITE_SCOPES, check
from agent.domains import browser, device_actions
from agent.runtime import memory
from agent.runtime.budget import (
    BudgetExceeded,
    RequestBudget,
    current_budget,
    native_tool,
)
from agent.runtime.catalog import initial_tools, loader
from agent.runtime.continuation import restore_evidence
from agent.runtime.finalization import incomplete_response
from agent.runtime.memory_media import tool_messages
from agent.runtime.message_content import text_content
from agent.runtime.middleware import SessionMiddleware, StreamGuard
from agent.runtime.progress import emit as emit_progress
from agent.runtime.prompt_budget import fit_history
from agent.runtime.read_tools import scoped_reads_enabled
from agent.runtime.results import TurnResult
from agent.runtime.turns import check_cancelled
from config.settings import settings
from tools.base import unavailable
from utils import trace as trace_mod
from utils.trace import record

_MAX_RESULT_CHARS = 8000
_MAX_TOTAL_RESULT_CHARS = 16000



class ToolSession:
    def __init__(self, model, config, tools, domains, outcome_adapter=None):
        self.budget = current_budget() or RequestBudget()
        self.output_limit = getattr(model, "max_tokens", None) or settings.agent_output_tokens
        self.model = model
        self.config = config
        self.principal = ((config or {}).get("configurable") or {}).get("principal")
        self.tools = initial_tools(tools, domains)
        self.initial_names = set(self.tools)
        self.domains = domains
        self.loaders = {f"load_{domain}_info_tools": domain for domain in domains}
        self.loaded: list[str] = []
        self.calls_used = 0
        self.result_chars = 0
        self.result_limit = _MAX_TOTAL_RESULT_CHARS
        self.outcome: TurnResult | None = None
        self.outcome_adapter = outcome_adapter
        self.continuation = None
        self.effects = {}
        self.observations = []
        self.receipts = []
        self.article_evidence = ArticleEvidence()
        self.used_stream_api = False
        self.finalizing = False
        self.traced_contracts = set()
        self.llm = self._bind_menu()

    def _bind_menu(self):
        # Keep discovery schemas in place; domain tools extend the request prefix.
        initial = [tool for name, tool in self.tools.items() if name in self.initial_names]
        loaded = [tool for name, tool in self.tools.items() if name not in self.initial_names]
        self.menu = [convert_to_openai_tool(tool) for tool in
                     [*initial, *(loader(domain) for domain in self.domains), *loaded]]
        return self.model

    def _handoff(self, reason, **data):
        if self.receipts:
            record("native", "handoff_stopped", reason=reason, **data)
            return self._result("工具操作结果已保留；其余请求尚未完成，请继续指定要处理的事项。")
        if self.calls_used:
            record("native", "handoff_stopped", reason=reason, **data)
            if reason == "unread_article_reference":
                return self._result("答复中的部分文章引用无法核实，本轮暂时无法提供可靠来源。")
            if reason == "article_body_missing":
                return self._result("暂时无法核实文章正文，不能据此回答文章内容。"
                                    "请重新指定要阅读的文章。")
            return self._result("本轮查询已停止，暂时没能完成请求。请明确下一步要处理的事项。")
        record("native", "task_stopped", reason=reason, **data)
        return self._result("本轮未能完成请求，没有执行未核实的操作。请补充目标或稍后重试。")

    def _result(self, reply):
        return self._with_receipts(TurnResult(reply))

    def _with_receipts(self, result):
        if not self.receipts and not self.effects:
            return result
        execution = {**self.effects, **(result.execution or {})}
        return TurnResult(result.reply, {**execution, "receipts": [
            *self.receipts, *(execution.get("receipts") or [])]})

    def _budget_stop(self, reason, **data):
        record("native", "budget_stop", reason=reason, **data)
        self.outcome = self._result("本轮操作已达到调用或上下文上限，已停止继续处理。"
                                    "请缩小请求范围后再试。")
        return self.outcome

    def _accept_calls(self, calls, round_index):
        for call in calls:
            if (not isinstance(call, dict) or not isinstance(call.get("name"), str)
                    or not call.get("name") or not isinstance(call.get("id"), str)
                    or not call.get("id") or not isinstance(call.get("args"), dict)):
                return "invalid_tool_call"
        if len({call["id"] for call in calls}) != len(calls):
            return "invalid_tool_call"
        return None

    def _tool_error(self, call, prompt, reason):
        available = sorted({*self.tools, *self.loaders})
        body = json.dumps({"error": reason, "available_tools": available})
        prompt.append(ToolMessage(content=body,
                                  tool_call_id=call["id"], status="error"))
        record("native", "tool_error", name=call["name"], reason=reason)
        return True

    def _invoke_tool(self, name, args, call=None):
        scope = TOOL_SCOPE.get(name)
        if scope in WRITE_SCOPES and not device_actions.authorized(self.config, name):
            record("native", "tool_denied", name=name)
            return None
        if scope in (SCOPE_READ_OWN, SCOPE_ADMIN_CONSOLE):
            uid = ((self.config or {}).get("configurable") or {}).get("user_id")
            if (not scoped_reads_enabled(self.principal, scope, uid)
                    or not check(self.principal, name).allowed):
                record("native", "tool_denied", name=name)
                return None
        try:
            if getattr(self.tools[name], "response_format", None) == "content_and_artifact":
                return self.tools[name].invoke({**call, "type": "tool_call"}, config=self.config)
            return self.tools[name].invoke(args, config=self.config)
        except (ToolException, ValueError) as exc:
            record("native", "tool_failed", name=name, error=type(exc).__name__)
            return unavailable(f"{name} 调用失败（{type(exc).__name__}），请如实说明。")

    def _append_result(self, call, prompt, round_index, attachments):
        name = call["name"]
        record("native", "tool_requested", name=name)
        if name in self.loaders:
            domain = self.loaders[name]
            if domain not in self.loaded:
                self.tools.update(self.domains[domain])
                self.loaded.append(domain)
                emit_progress(self.config, f"正在加载工具领域：{domain}", key=f"domain:{domain}")
                record("native", "load_domain", domain=domain)
                self.llm = self._bind_menu()
            prompt.append(ToolMessage(content=f"{domain} 工具已加载。",
                                      tool_call_id=call["id"]))
            return True
        if name not in self.tools:
            return self._tool_error(call, prompt, "unknown_tool")
        if self.result_chars >= _MAX_TOTAL_RESULT_CHARS:
            self._budget_stop("result_budget")
            return False
        args = call["args"]
        self.calls_used += 1
        emit_progress(self.config, f"正在调用工具：{name}", key=f"tool:{call['id']}")
        with native_tool(self.budget):
            body = self._invoke_tool(name, args, call)
        emit_progress(self.config, f"工具调用已返回：{name}", key=f"tool_done:{call['id']}")
        if isinstance(body, TurnResult):
            self._prepared_result(body, call, prompt, round_index)
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
        body = text_content(message.content) if message else str(body)
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
        messages = tool_messages(message, body, call["id"])
        prompt.append(messages[0])
        attachments.extend(messages[1:])
        self.result_chars += len(body)
        record("native", "call", name=name, args=args, round=round_index,
               kind=kind, result=trace_mod.tool_result_text(body, name))
        record("native", "tool_result", name=name, result_chars=len(body))
        return True

    def _prepared_result(self, body, call, prompt, round_index):
        # Confirmation tokens stay in the terminal outcome, never in the prompt or trace.
        adapted = self.outcome_adapter(body) if self.outcome_adapter else body
        execution = adapted.execution or {}
        if execution.get("pending_event"):
            self.outcome_adapter = None
        kind = ("prepared" if execution.get("pending_confirm") else
                "noop" if execution.get("noop_note") else
                "cancelled" if execution.get("pending_event") else "terminal")
        record("native", "call", name=call["name"], round=round_index, kind=kind)
        if kind == "noop":
            self.effects.update(execution)
            prompt.append(ToolMessage(content=adapted.reply, name=call["name"],
                                      tool_call_id=call["id"]))
        else:
            self.outcome = self._with_receipts(adapted)

    def _restore_domains(self, prompt):
        observed = {message.name for message in prompt
                    if isinstance(message, ToolMessage) and message.status != "error"}
        observed.difference_update(self.initial_names)
        restored = [domain for domain, tools in self.domains.items()
                    if domain not in self.loaded and observed.intersection(tools)]
        for domain in restored:
            self.tools.update(self.domains[domain])
            self.loaded.append(domain)
        if restored:
            self.llm = self._bind_menu()
            record("native", "restore_domains", domains=restored)

    @staticmethod
    def _complete_tool_arguments(response):
        # LangChain deliberately repairs partial JSON for streaming previews.
        # An executable call requires the complete original JSON object.
        for call in getattr(response, "tool_call_chunks", ()):
            try:
                args = json.loads(call.get("args", ""))
            except (ValueError, TypeError):
                return False
            if not isinstance(args, dict):
                return False
        calls = getattr(response, "tool_calls", ()) or getattr(response, "invalid_tool_calls", ())
        return not calls or response.response_metadata.get("finish_reason") != "length"

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
        execution = result.execution
        terminal_key = ("confirm_text" if execution and execution.get("pending_confirm") else
                        "noop_text" if execution and execution.get("noop_note") else None)
        if terminal_key:
            execution = {**execution, terminal_key: missing}
        if missing:
            visible_parts.append(missing)
            if terminal_key is None:
                yield AIMessageChunk(content=missing)
        return TurnResult("".join(visible_parts), execution)

    def _validate_response(self, response):
        """Apply business evidence and budget rules before framework dispatch."""
        incomplete = incomplete_response(response)
        if incomplete:
            event, reply = incomplete
            if event == "invalid_tool_call" and self.receipts:
                reply = "已有操作回执已保留；后续工具请求未完整，未执行新的操作。"
            record("native", event, round=self._round_index,
                   count=len(getattr(response, "invalid_tool_calls", ()) or ()))
            return None, self._result(reply)
        calls = getattr(response, "tool_calls", None) or []
        if not calls:
            reply = text_content(response.content).strip()
            from tools.base import SITE_BASE

            issue = self.article_evidence.issue(reply, SITE_BASE)
            if issue:
                return None, self._handoff(issue, tool_calls=self.calls_used)
            evidence = self.article_evidence
            if evidence.searched and not evidence.documents:
                if evidence.failed and not evidence.found:
                    return None, self._result("文章检索暂时不可用，当前无法核实文章内容。")
                if evidence.empty and not evidence.found:
                    return None, self._result("暂未从公开文章中检索到相关资料。")
            reply = evidence.sources(reply, SITE_BASE)
            return None, (self._result(reply) if reply else
                          self._handoff("empty", tool_calls=self.calls_used))
        if self.finalizing:
            return None, self._budget_stop("finalization_tool_call", tool_calls=self.calls_used)
        reason = self._accept_calls(calls, self._round_index)
        if reason:
            return None, self._handoff(reason, tool_calls=self.calls_used)
        try:
            self.budget.attempt_tools(len(calls))
        except BudgetExceeded as exc:
            return None, self._budget_stop(exc.reason, tool_calls=self.calls_used)
        return calls, None

    def stream(self, prompt, *, continuation=None):
        """Adapt framework message events to the current transport result contract."""
        self._round_index = 0
        self.stream_guard = StreamGuard(self)
        middleware = SessionMiddleware(self)
        if continuation is not None:
            for domain in continuation.loaded:
                if domain in self.domains:
                    self.tools.update(self.domains[domain])
                    self.loaded.append(domain)
            self.llm = self._bind_menu()
            self.article_evidence = restore_evidence(continuation.evidence)
            self.result_chars = continuation.result_chars
            try:
                self.budget.attempt_tools(len(continuation.remaining))
            except BudgetExceeded as exc:
                self._budget_stop(exc.reason, tool_calls=self.calls_used)
                return (yield from self._stream_terminal(self.outcome, []))
        else:
            self._restore_domains(prompt)
            prompt = fit_history(prompt, self.principal, self.menu, self.budget.limits,
                                 self.output_limit)
        # Message streaming uses a framework waiter in addition to the node worker.
        config = {**(self.config or {}), "max_concurrency": 2,
                  "recursion_limit": self.budget.limits.model_calls * 6 + 12,
                  "callbacks": [*((self.config or {}).get("callbacks") or []), self.stream_guard]}
        saver = InMemorySaver() if continuation is not None else None
        system = prompt[0] if prompt and isinstance(prompt[0], SystemMessage) else None
        messages = prompt[1:] if system is not None else prompt
        middleware.system_message = system
        graph = create_agent(self.model, tools=[], system_prompt=system,
                             middleware=[middleware], checkpointer=saver)
        inputs = {"messages": messages}
        if continuation is not None:
            config["configurable"] = {"thread_id": "confirmation-resume",
                                      **config.get("configurable", {})}
            # The outer store still owns the old signed cursor during this first
            # migration stage. Standard graph routing dispatches its unanswered calls.
            middleware.exchange = list(prompt)
            middleware.calls = list(continuation.remaining)
            middleware.attachments = list(continuation.attachments)
            graph.update_state(config, inputs, as_node="SessionMiddleware.after_model")
            inputs = None
        stream = graph.stream(inputs, config, stream_mode=["messages", "updates"])
        visible = []
        self.used_stream_api = True
        try:
            for mode, data in stream:
                check_cancelled(self.config)
                if mode != "messages":
                    continue
                chunk, metadata = data
                if (metadata.get("langgraph_node") != "model"
                        or self.article_evidence.searched or self.article_evidence.documents):
                    continue
                text = text_content(chunk.content)
                if text:
                    visible.append(text)
                if text or chunk.usage_metadata:
                    yield chunk.model_copy(update={"content": text})
        except BudgetExceeded as exc:
            self._budget_stop(exc.reason, tool_calls=self.calls_used)
        finally:
            self.stream_guard.closed = True
            stream.close()
        if self.outcome is None:
            raise RuntimeError("Framework Agent ended without a terminal result")
        return (yield from self._stream_terminal(self.outcome, visible))

    def run(self, prompt):
        runner = self.stream(prompt)
        while True:
            try:
                next(runner)
            except StopIteration as stop:
                return stop.value
