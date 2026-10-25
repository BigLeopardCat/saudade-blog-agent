"""Application policy on the standard LangChain model and tool execution hooks."""

import json
import time
from threading import Condition

from langchain.agents.middleware import AgentMiddleware, hook_config
from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.messages import AIMessage, AIMessageChunk, RemoveMessage, ToolMessage
from langgraph.types import Command

from agent.llm_usage import usage_fields
from agent.runtime import trace_context
from agent.runtime.budget import native_call
from agent.runtime.control import AgentCancelled, model_options
from agent.runtime.execution_state import capture, restore
from agent.runtime.finalization import final_prompt
from agent.runtime.memory_media import hydrate, stage
from agent.runtime.message_content import text_content
from agent.runtime.progress import emit
from agent.runtime.results import pause_notice
from agent.runtime.turns import check_cancelled
from config.settings import settings
from utils.trace import record


class StreamGuard(BaseCallbackHandler):
    """Observe standard model callbacks; cancellation also closes provider streaming."""

    raise_error = True
    run_inline = True

    def __init__(self, session):
        self.session = session
        self.aggregate = None
        self.closed = False

    def on_chat_model_start(self, serialized, messages, **kwargs):
        self.aggregate = None
        self._check()

    def _check(self):
        if self.closed:
            raise AgentCancelled()
        check_cancelled(self.session.config)

    def on_llm_new_token(self, token, *, chunk=None, **kwargs):
        self._check()
        message = getattr(chunk, "message", None)
        if isinstance(message, AIMessageChunk):
            self.aggregate = message if self.aggregate is None else self.aggregate + message


class SessionMiddleware(AgentMiddleware):
    """Keep permissions, budgets and product outcomes outside framework scheduling."""

    def __init__(self, session):
        self.session = session
        self.exchange = []
        self.calls = []
        self.attachments = []
        self._order = Condition()
        self._next = 0
        self.system_message = None
        self._failed = False
        self.cached = {}

    @hook_config(can_jump_to=["end", "tools"])
    def before_model(self, state, runtime):
        session = self.session
        check_cancelled(session.config)
        if state.get("resume"):
            return self._resume(state)
        saved = capture(session)
        if session.outcome is not None:
            saved["jump_to"] = "end"
        return saved

    def _resume(self, state):
        session = self.session
        facts = state["resume"]
        restore(session, state)
        proposal = (session.outcome.execution or {}) if session.outcome else {}
        if facts["jti"] != proposal.get("pending_action", {}).get("jti"):
            raise ValueError("执行回执与 Agent 待确认操作不一致")
        messages = list(state["messages"])
        index = next(i for i in range(len(messages) - 1, -1, -1)
                     if isinstance(messages[i], AIMessage) and messages[i].tool_calls)
        self.calls = messages[index].tool_calls
        answered = {message.tool_call_id: message for message in messages[index + 1:]
                    if isinstance(message, ToolMessage)}
        awaiting = answered[session.awaiting_call]
        visible = {"status": "executed", "result": facts["reply"], "receipts": [
            {key: row[key] for key in ("tool", "args", "result", "action") if key in row}
            for row in facts["execution"]["receipts"]]}
        receipt = awaiting.model_copy(update={"content": json.dumps(visible, ensure_ascii=False),
                                              "artifact": None})
        deferred = [message for message in answered.values()
                    if (message.artifact or {}).get("phase") == "deferred"]
        self.cached = {ident: message for ident, message in answered.items()
                       if message not in deferred}
        self.cached[session.awaiting_call] = receipt
        self.exchange = [*messages[:index + 1], *self.cached.values()]
        session.outcome, session.awaiting_call = None, None
        saved = {**capture(session), "resume": None,
                 "messages": [receipt, *(RemoveMessage(id=m.id) for m in deferred)]}
        if deferred:
            self.attachments = [message for message in messages[index + 1:]
                                if not isinstance(message, ToolMessage)]
            saved["messages"].extend(RemoveMessage(id=m.id) for m in self.attachments)
            session.budget.attempt_tools(len(deferred))
            self._next = 0
            saved["jump_to"] = "tools"
        return saved

    def wrap_model_call(self, request, handler):
        session = self.session
        prompt = [*([request.system_message] if request.system_message else []),
                  *hydrate(request.messages, session.transient)]
        final = final_prompt(session.budget, prompt, session.menu, session.output_limit,
                             results_full=session.result_chars >= session.result_limit)
        model = session.model
        if final:
            prompt, reason, output = final
            model = model.bind(max_tokens=output) if output < session.output_limit else model
            session.menu, session.finalizing, session.output_limit = [], True, output
            record("native", "finalizing", reason=reason, output_limit=output)
        label = "正在整理已取得的结果…" if session.finalizing else "正在处理请求…"
        emit(session.config, label, key=f"model:{session._round_index}")
        record("native", "llm_start", round=session._round_index, engine="langchain",
               prompt_chars=sum(len(text_content(message.content)) for message in prompt),
               streaming=True)
        trace_context.tools(session.menu, session.traced_contracts)
        started, response = time.monotonic(), None
        try:
            with native_call(session.budget, prompt, session.menu,
                             output_limit=session.output_limit) as ticket:
                messages = prompt[1:] if request.system_message else prompt
                result = handler(request.override(
                    model=model, messages=messages, tools=session.menu,
                    tool_choice="auto" if session.menu else None,
                    model_settings={**({"parallel_tool_calls": False} if session.menu else {}),
                                    **model_options(settings.llm_timeout)}))
                response = result.result[-1]
                session.budget.settle(ticket, response)
                return result
        finally:
            record("native", "llm_done" if response is not None else "llm_error",
                   round=session._round_index, engine="langchain",
                   duration_s=round(time.monotonic() - started, 2), streaming=True,
                   finish_reason=(getattr(response, "response_metadata", {}) or {}).get(
                       "finish_reason"), **usage_fields(response))

    @hook_config(can_jump_to=["end"])
    def after_model(self, state, runtime):
        session = self.session
        response = state["messages"][-1]
        aggregate = session.stream_guard.aggregate
        if aggregate is not None and not session._complete_tool_arguments(aggregate):
            response = response.model_copy(update={
                "tool_calls": [], "invalid_tool_calls": [{"name": None, "args": "",
                    "id": None, "error": "incomplete_tool_arguments",
                    "type": "invalid_tool_call"}]})
        check_cancelled(session.config)
        calls, outcome = session._validate_response(response)
        system = [self.system_message] if self.system_message else []
        self.exchange = [*system, *state["messages"]]
        self.calls, self.attachments = calls or [], []
        self._next = 0
        self.cached = {}
        session._round_index += 1
        if outcome is not None:
            session.outcome = outcome
            return {**capture(session), "jump_to": "end"}
        return capture(session)

    def wrap_tool_call(self, request, handler):
        return self._tool_result(request.tool_call)

    def _tool_result(self, call):
        session = self.session
        check_cancelled(session.config)
        if call["id"] in self.cached:
            message = self.cached[call["id"]]
        elif session.outcome is not None:
            message = ToolMessage(content="尚未执行，等待当前操作的确认。",
                                  tool_call_id=call["id"], artifact={"phase": "deferred"})
        else:
            before = len(self.exchange)
            session._append_result(call, self.exchange, max(0, session._round_index - 1),
                                   self.attachments)
            execution = (session.outcome.execution or {}) if session.outcome else {}
            if execution.get("pending_confirm"):
                session.awaiting_call = call["id"]
                if call["id"] != self.calls[-1]["id"]:
                    session.outcome = pause_notice(session.outcome)
            message = (self.exchange[-1] if len(self.exchange) > before else ToolMessage(
                content="操作已准备，等待用户决定。", name=call["name"], tool_call_id=call["id"],
                artifact={"phase": "awaiting_approval"}))
        if self.calls and call["id"] == self.calls[-1]["id"] and self.attachments:
            return Command(update={"messages": [message, *stage(self.attachments,
                                                               session.transient)]})
        return message


class OrderedToolsMiddleware(AgentMiddleware):
    """Serialize the complete policy chain for each model's ordered tool batch."""

    def __init__(self, policy):
        self.policy = policy

    def wrap_tool_call(self, request, handler):
        policy = self.policy
        with policy._order:
            while (not policy._failed
                   and policy.calls[policy._next]["id"] != request.tool_call["id"]):
                check_cancelled(policy.session.config)
                policy._order.wait(0.05)
            if policy._failed:
                return ToolMessage(content="前序调用失败，本项未执行。", status="error",
                                   tool_call_id=request.tool_call["id"])
            try:
                return handler(request)
            except BaseException:
                policy._failed = True
                raise
            finally:
                policy._next += 1
                policy._order.notify_all()
