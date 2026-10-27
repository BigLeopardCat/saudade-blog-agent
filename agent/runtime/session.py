"""Application context and result policy for the standard LangChain Agent.

The framework schedules model and tool rounds. The application owns authorized
capabilities, evidence, budgets and the existing signed confirmation protocol.
"""

import hashlib
import json

from langchain_core.messages import AIMessageChunk, ToolMessage
from langchain_core.tools import ToolException
from langchain_core.utils.function_calling import convert_to_openai_tool
from pydantic import ValidationError

from agent.runtime import memory
from agent.runtime.budget import (
    BudgetExceeded,
    RequestBudget,
    current_budget,
    native_tool,
)
from agent.runtime.catalog import loader
from agent.runtime.finalization import incomplete_response
from agent.runtime.memory_media import tool_messages
from agent.runtime.message_content import text_content
from agent.runtime.middleware import StreamGuard
from agent.runtime.progress import emit as emit_progress
from agent.runtime.results import TurnResult
from agent.runtime.tool_errors import validation_result
from agent.runtime.turns import AgentCancelled, check_cancelled
from agent.runtime.visible_text import VisibleText
from config.settings import settings
from tools.base import unavailable
from utils import trace as trace_mod
from utils.trace import record

_MAX_RESULT_CHARS = 8000
_MAX_TOTAL_RESULT_CHARS = 16000



class ToolSession:
    def __init__(self, model, config, tools, domains, outcome_adapter=None, *,
                 store=None, isolated=False, descriptions=None, middleware=(), history_access=None):
        self.budget = current_budget() or RequestBudget()
        self.output_limit = getattr(model, "max_tokens", None) or settings.agent_output_tokens
        self.model = model
        self.config = config
        self.principal = ((config or {}).get("configurable") or {}).get("principal")
        self.tools = dict(tools)
        self.initial_names = set(self.tools)
        self.domains = domains
        self.loaders = {f"load_{domain}_info_tools": domain for domain in domains}
        self.loaded: list[str] = []
        self.calls_used = 0
        self.result_chars = 0
        self.result_limit = _MAX_TOTAL_RESULT_CHARS
        self.outcome: TurnResult | None = None
        self.outcome_adapter = outcome_adapter
        self.store, self.isolated = store, isolated
        self.approval = None
        self.awaiting_call = None
        self.framework_state = None
        self.task = None
        self.input_inbox = None
        self.inputs_active = False
        self.input_events_enabled = False
        self.input_messages = []
        self.applied_inputs = set()
        self.recovering = False
        self.recovery_state = None
        self.transient = {}
        self.effects = {}
        self.observations = []
        self.receipts = []
        self.suppress_output = False
        self.descriptions = descriptions or {}
        self.history_access = history_access
        self.render_result = self._render_result
        self.middleware = [factory(self) for factory in middleware]
        self.used_stream_api = False
        self.finalizing = False
        self.traced_contracts = set()
        self.llm = self._bind_menu()

    @property
    def applied_messages(self):
        return [message for message in self.input_messages
                if message.additional_kwargs["task_input"]["id"] in self.applied_inputs]

    def _bind_menu(self):
        # Keep discovery schemas in place; domain tools extend the request prefix.
        initial = [tool for name, tool in self.tools.items() if name in self.initial_names]
        loaded = [tool for name, tool in self.tools.items() if name not in self.initial_names]
        discovery = [loader(domain, self.descriptions.get(domain)) for domain in self.domains]
        self.menu = [convert_to_openai_tool(tool) for tool in [*initial, *discovery, *loaded]]
        return self.model

    def _handoff(self, reason, **data):
        if self.receipts:
            record("native", "handoff_stopped", reason=reason, **data)
            return self._result("工具操作结果已保留；其余请求尚未完成，请继续指定要处理的事项。")
        if self.calls_used:
            record("native", "handoff_stopped", reason=reason, **data)
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

    def _invoke_tool(self, name, args, call=None, config=None):
        try:
            if getattr(self.tools[name], "response_format", None) == "content_and_artifact":
                return self.tools[name].invoke({**call, "type": "tool_call"},
                                               config=config or self.config)
            return self.tools[name].invoke(args, config=config or self.config)
        except ValidationError as exc:
            record("native", "tool_failed", name=name, error="invalid_arguments")
            return validation_result(exc)
        except ToolException as exc:
            from tools.result import invalid_args
            return invalid_args(str(exc))
        except ValueError as exc:
            record("native", "tool_failed", name=name, error=type(exc).__name__)
            return unavailable(f"{name} 调用失败（{type(exc).__name__}），请如实说明。")

    def _append_result(self, call, prompt, round_index, attachments, *, config=None):
        name = call["name"]
        record("native", "tool_requested", name=name, call_id=call["id"])
        if name in self.loaders:
            domain = self.loaders[name]
            if domain not in self.loaded:
                self.tools.update(self.domains[domain])
                self.loaded.append(domain)
                emit_progress(self.config, f"正在加载工具领域：{domain}", key=f"domain:{domain}")
                record("native", "load_domain", domain=domain, call_id=call["id"])
                self.llm = self._bind_menu()
            prompt.append(ToolMessage(content=f"{domain} 工具已加载。",
                                      name=name, tool_call_id=call["id"]))
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
            body = self._invoke_tool(name, args, call, config=config)
        if isinstance(body, TurnResult):
            self._prepared_result(body, call, prompt, round_index)
            return True
        if body is None:
            prompt.append(ToolMessage(content="无权读取此数据。", tool_call_id=call["id"]))
            return True
        message = body if isinstance(body, ToolMessage) else None
        artifact = message.artifact if message and isinstance(message.artifact, dict) else {}
        kind = artifact.get("kind", "unknown") if message else getattr(body, "kind", "unknown")
        if message and message.status == "error":
            kind = "error"
        self.observations.extend(memory.observation(call, body))
        remaining = _MAX_TOTAL_RESULT_CHARS - self.result_chars
        body = self.render_result(call, body, min(_MAX_RESULT_CHARS, remaining))
        messages = tool_messages(message, body, call["id"])
        prompt.append(messages[0].model_copy(update={"name": name, "artifact": {
            **(messages[0].artifact or {}), "kind": kind}}))
        attachments.extend(messages[1:])
        self.result_chars += len(body)
        record("native", "call", name=name, args=args, call_id=call["id"], round=round_index,
               kind=kind, result=trace_mod.tool_result_text(body, name))
        record("native", "tool_result", name=name, call_id=call["id"], result_chars=len(body))
        return True

    @staticmethod
    def _render_result(call, value, limit):
        body = text_content(value.content) if isinstance(value, ToolMessage) else str(value)
        if len(body) > limit:
            marker = "\n[结果截断，请缩小检索范围]"
            body = body[:max(0, limit - len(marker))] + marker
        return body

    def _prepared_result(self, body, call, prompt, round_index):
        # Confirmation tokens stay in the terminal outcome, never in the prompt or trace.
        adapted = self.outcome_adapter(body) if self.outcome_adapter else body
        execution = adapted.execution or {}
        if execution.get("pending_event"):
            self.outcome_adapter = None
        kind = ("prepared" if execution.get("pending_confirm") else
                "noop" if execution.get("noop_note") else
                "cancelled" if execution.get("pending_event") else "terminal")
        args_hash = hashlib.sha256(json.dumps(
            call["args"], sort_keys=True, ensure_ascii=False, separators=(",", ":")
        ).encode()).hexdigest()
        record("native", "call", name=call["name"], args_hash=args_hash,
               call_id=call["id"], round=round_index, kind=kind)
        if kind == "noop" or kind == "cancelled":
            self.effects.update(execution)
            message = ToolMessage(content=adapted.reply, name=call["name"],
                                  tool_call_id=call["id"], artifact={
                                      "kind": "ok", "effects": dict(self.effects)})
            prompt.append(message)
            self.observations.extend(memory.observation(call, message))
        else:
            self.outcome = self._with_receipts(adapted)

    def _restore_domains(self, prompt):
        observed = {message.name for message in prompt
                    if isinstance(message, ToolMessage) and message.status != "error"}
        observed.difference_update(self.initial_names)
        restored = [domain for domain, tools in self.domains.items()
                    if domain not in self.loaded and (observed.intersection(tools)
                    or f"load_{domain}_info_tools" in observed)]
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

    def stream(self, prompt, *, resume=None, approval_input=(), recover=False, client_result=None):
        """Adapt framework events to the current transport result contract."""
        from agent.runtime.execution import FrameworkExecution

        self._round_index = 0
        self.stream_guard = StreamGuard(self)
        execution = FrameworkExecution(self, prompt)
        stream = execution.stream(resume=resume, approval_input=approval_input, recover=recover,
                                  client_result=client_result)
        visible = []
        boundary, model_key = VisibleText(), None
        self.used_stream_api = True
        reported_inputs = set()
        try:
            for mode, data in stream:
                check_cancelled(self.config)
                if mode == "updates" and isinstance(data, dict):
                    from agent.runtime.task_input_policy import events as input_events
                    for input_event in input_events(self, data, reported_inputs):
                        if self.input_events_enabled:
                            yield input_event
                    from agent.runtime.client_actions import paused_result
                    paused = paused_result(data)
                    if paused is not None:
                        self.outcome = paused
                if mode != "messages":
                    continue
                chunk, metadata = data
                if (metadata.get("langgraph_node") != "model"
                        or self.suppress_output):
                    continue
                key = (metadata.get("langgraph_step"), chunk.id)
                if key != model_key:
                    tail = boundary.finish()
                    if tail:
                        visible.append(tail)
                        yield AIMessageChunk(content=tail)
                    boundary, model_key = VisibleText(), key
                text = boundary.feed(text_content(chunk.content))
                if text:
                    visible.append(text)
                if text or chunk.usage_metadata:
                    yield chunk.model_copy(update={"content": text})
        except BudgetExceeded as exc:
            self._budget_stop(exc.reason, tool_calls=self.calls_used)
        except (AgentCancelled, GeneratorExit, TimeoutError):
            stream.close()
            execution.preserve_lifecycle()
            raise
        finally:
            self.stream_guard.closed = True
            stream.close()
        execution.promote()
        tail = boundary.finish()
        if tail:
            visible.append(tail)
            yield AIMessageChunk(content=tail)
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
