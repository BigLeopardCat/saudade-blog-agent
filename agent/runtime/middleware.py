"""Application policy on the standard LangChain model and tool execution hooks."""

import time
from threading import Condition

from langchain.agents.middleware import AgentMiddleware, hook_config
from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.messages import AIMessageChunk, ToolMessage
from langgraph.types import Command

from agent.llm_usage import usage_fields
from agent.runtime import trace_context
from agent.runtime.budget import native_call
from agent.runtime.continuation import ToolContinuation, evidence_state, pause_notice
from agent.runtime.control import AgentCancelled, model_options
from agent.runtime.finalization import final_prompt
from agent.runtime.message_content import text_content
from agent.runtime.progress import emit
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

    @hook_config(can_jump_to=["end"])
    def before_model(self, state, runtime):
        check_cancelled(self.session.config)
        return {"jump_to": "end"} if self.session.outcome is not None else None

    def wrap_model_call(self, request, handler):
        session = self.session
        prompt = [*([request.system_message] if request.system_message else []), *request.messages]
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
        session._round_index += 1
        if outcome is not None:
            session.outcome = outcome
            return {"jump_to": "end"}
        return None

    def wrap_tool_call(self, request, handler):
        # Preparing a later operation must wait for the preceding result. The
        # framework owns dispatch; this lock protects application policy state.
        with self._order:
            while (not self._failed
                   and self.calls[self._next]["id"] != request.tool_call["id"]):
                check_cancelled(self.session.config)
                self._order.wait(0.05)
            if self._failed:
                return ToolMessage(content="前序调用失败，本项未执行。", status="error",
                                   tool_call_id=request.tool_call["id"])
            try:
                return self._tool_result(request.tool_call)
            except BaseException:
                self._failed = True
                raise
            finally:
                self._next += 1
                self._order.notify_all()

    def _tool_result(self, call):
        session = self.session
        check_cancelled(session.config)
        # A signed proposal ends this HTTP turn. The framework still completes
        # siblings' message envelopes; their business tools must wait for approval.
        if session.outcome is not None:
            return ToolMessage(content="尚未执行，等待当前操作的确认。", tool_call_id=call["id"])
        before = len(self.exchange)
        session._append_result(call, self.exchange, session._round_index - 1, self.attachments)
        if session.outcome is not None:
            self._suspend(call)
        message = (self.exchange[-1] if len(self.exchange) > before else ToolMessage(
            content="操作已准备，等待用户决定。", name=call["name"], tool_call_id=call["id"]))
        if self.calls and call["id"] == self.calls[-1]["id"] and self.attachments:
            return Command(update={"messages": [message, *self.attachments]})
        return message

    def _suspend(self, call):
        session = self.session
        execution = session.outcome.execution or {}
        if not execution.get("pending_confirm"):
            return
        index = next(i for i, item in enumerate(self.calls) if item["id"] == call["id"])
        remaining = self.calls[index + 1:]
        if remaining:
            session.outcome = pause_notice(session.outcome)
        cursor = ToolContinuation(self.exchange, call, remaining, self.attachments,
                                  list(session.loaded), evidence_state(session.article_evidence),
                                  session.result_chars)
        session.continuation = cursor.checkpoint(session.config, execution["pending_action"]["jti"])
