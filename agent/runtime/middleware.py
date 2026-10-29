"""Application policy on the standard LangChain model and tool execution hooks."""

import json
import time
from dataclasses import replace
from threading import Condition

from langchain.agents.middleware import AgentMiddleware, hook_config
from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.messages import AIMessage, AIMessageChunk, RemoveMessage, ToolMessage
from langgraph.errors import GraphInterrupt
from langgraph.graph.message import REMOVE_ALL_MESSAGES
from langgraph.types import Command

from agent.llm_usage import usage_fields
from agent.runtime import memory, trace_context
from agent.runtime.budget import BudgetExceeded, native_call
from agent.runtime.control import AgentCancelled, model_options
from agent.runtime.execution_state import adopt_command, capture, restore
from agent.runtime.memory_media import hydrate, stage
from agent.runtime.message_content import text_content
from agent.runtime.progress import emit
from agent.runtime.prompt_budget import fit_history
from agent.runtime.results import pause_notice
from agent.runtime.task_input_policy import additions as input_additions
from agent.runtime.task_input_policy import model_messages, replace_unpublished_proposal
from agent.runtime.tool_execution import append_result
from agent.runtime.turns import check_cancelled
from config.settings import settings
from utils.trace import record


def _model_name(model=None, response=None):
    metadata = getattr(response, "response_metadata", {}) or {}
    return next((value for value in (metadata.get("model_name"), metadata.get("model"),
                getattr(model, "model_name", None), getattr(model, "model", None))
                 if isinstance(value, str) and value), None)


class StreamGuard(BaseCallbackHandler):
    """Observe standard model callbacks; cancellation also closes provider streaming."""

    raise_error = True
    run_inline = True

    def __init__(self, session):
        self.session = session
        self.aggregate = None
        self.closed = False
        self.started = None
        self.first_token = False

    def on_chat_model_start(self, serialized, messages, **kwargs):
        self.aggregate = None
        self.started, self.first_token = time.monotonic(), False
        self._check()

    def _check(self):
        if self.closed:
            raise AgentCancelled()
        check_cancelled(self.session.config)

    def on_llm_new_token(self, token, *, chunk=None, **kwargs):
        self._check()
        message = getattr(chunk, "message", None)
        if isinstance(message, AIMessageChunk):
            meaningful = bool(token or message.content or message.tool_call_chunks
                              or message.additional_kwargs.get("reasoning_content"))
            if meaningful and not self.first_token and self.started is not None:
                self.first_token = True
                record("native", "model_first_token",
                       round=getattr(self.session, "_round_index", None),
                       duration_s=time.monotonic() - self.started)
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
        self._paused = False
        self.cached = {}
        self._recovered = False

    def _recover(self, state):
        if not self.session.recovering or self._recovered:
            return
        state = self.session.recovery_state or state
        messages = list(state.get("messages") or [])
        index = next((i for i in range(len(messages) - 1, -1, -1)
                      if isinstance(messages[i], AIMessage) and messages[i].tool_calls), None)
        self.exchange = [*([self.system_message] if self.system_message else []), *messages]
        if index is not None:
            self.calls = messages[index].tool_calls
            self.cached = {m.tool_call_id: m for m in messages[index + 1:]
                           if isinstance(m, ToolMessage)
                           and (m.artifact or {}).get("phase") != "deferred"}
        self._recovered = True

    @hook_config(can_jump_to=["end", "tools"])
    def before_model(self, state, runtime):
        session = self.session
        self._recover(state)
        check_cancelled(session.config)
        restore(session, state)
        if state.get("resume"):
            return self._resume(state)
        replacement = replace_unpublished_proposal(session, state)
        if replacement:
            return {**capture(session), "messages": replacement}
        saved = capture(session)
        if session.outcome is not None:
            saved["jump_to"] = "end"
        elif session.isolated and session.state["effects"].get("pending_event"):
            # Finish the isolated graph before transferring its standard state.
            saved["jump_to"] = "end"
        elif session.recovering and any(call["id"] not in self.cached for call in self.calls):
            # A lifecycle boundary can be saved halfway through a native batch.
            session.budget.attempt_tools(sum(call["id"] not in self.cached for call in self.calls))
            saved["jump_to"] = "tools"
        if "jump_to" not in saved:
            if session._round_index == 0 and session.fresh_task:
                prompt = [*([self.system_message] if self.system_message else []),
                          *hydrate(state["messages"], session.transient)]
                fitted = fit_history(prompt, session.principal, session.menu,
                    session.budget.limits, session.output_limit,
                    tool_access=session.history_access, budget=session.budget)
                if fitted != prompt:
                    body = fitted[1:] if self.system_message else fitted
                    saved["messages"] = [RemoveMessage(id=REMOVE_ALL_MESSAGES),
                                         *stage(body, session.transient)]
            extra = input_additions(session, state)
            if extra:
                saved.setdefault("messages", []).extend(extra)
        return saved

    def _resume(self, state):
        session = self.session
        facts = state["resume"]
        session.state["resume"] = None
        proposal = (session.outcome.execution or {}) if session.outcome else {}
        if facts["jti"] != proposal.get("pending_action", {}).get("jti"):
            raise ValueError("执行回执与 Agent 待确认操作不一致")
        session.state["effects"].update({key: value for key, value in facts["execution"].items()
                                          if key != "receipts"})
        messages = list(state["messages"])
        index = next(i for i in range(len(messages) - 1, -1, -1)
                     if isinstance(messages[i], AIMessage) and messages[i].tool_calls)
        self.calls = messages[index].tool_calls
        answered = {message.tool_call_id: message for message in messages[index + 1:]
                    if isinstance(message, ToolMessage)}
        awaiting = answered[session.state["awaiting_call"]]
        visible = {"status": "executed", "result": facts["reply"], "receipts": [
            {key: row[key] for key in ("tool", "args", "result", "action") if key in row}
            for row in facts["execution"]["receipts"]]}
        receipt = awaiting.model_copy(update={"content": json.dumps(visible, ensure_ascii=False),
                                              "artifact": None})
        deferred = [message for message in answered.values()
                    if (message.artifact or {}).get("phase") == "deferred"]
        self.cached = {ident: message for ident, message in answered.items()
                       if message not in deferred}
        self.cached[session.state["awaiting_call"]] = receipt
        self.exchange = [*messages[:index + 1], *self.cached.values()]
        session.outcome, session.state["awaiting_call"] = None, None
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
        # Recovery can retry the model node directly, without rerunning its
        # preceding before_model checkpoint. Persist these additions together
        # with this model's response through the framework's standard reducer.
        extra_inputs = input_additions(session, request.state)
        if extra_inputs:
            request = request.override(messages=[*request.messages, *extra_inputs])
        prompt = [*([request.system_message] if request.system_message else []),
                  *model_messages(hydrate(request.messages, session.transient))]
        if session.state["result_chars"] >= session.result_limit:
            raise BudgetExceeded("result_budget")
        model = request.model
        selected_model = _model_name(model)
        emit(session.config, "正在处理请求…", key="model_start")
        trace_context.tools(session.menu, session.traced_contracts)
        started, response, invoked = time.monotonic(), None, False
        tool_choice = "auto" if session.menu else None
        try:
            with native_call(session.budget, prompt, session.menu, model_key=request.model,
                             output_limit=session.output_limit) as ticket:
                invoked = True
                record("native", "llm_start", round=session._round_index, engine="langchain",
                       prompt_chars=sum(len(text_content(message.content)) for message in prompt),
                       streaming=True, model=selected_model)
                messages = prompt[1:] if request.system_message else prompt
                result = handler(request.override(
                    model=model, messages=messages, tools=session.menu,
                    tool_choice=tool_choice,
                    model_settings=model_options(settings.llm_timeout)))
                response = result.result[-1]
                session.budget.settle(ticket, response)
                return (replace(result, result=[*extra_inputs, *result.result])
                        if extra_inputs else result)
        finally:
            response_model = _model_name(response=response)
            if invoked:
                record("native", "llm_done" if response is not None else "llm_error",
                       round=session._round_index, engine="langchain",
                       duration_s=round(time.monotonic() - started, 2), streaming=True,
                       model=selected_model or response_model, response_model=response_model,
                       finish_reason=(getattr(response, "response_metadata", {}) or {}).get(
                           "finish_reason"),
                       tool_calls=[call["name"] for call in getattr(response, "tool_calls", ())],
                       invalid_tool_calls=bool(getattr(response, "invalid_tool_calls", ())),
                       **usage_fields(response))

    @hook_config(can_jump_to=["end", "model"])
    def after_model(self, state, runtime):
        session = self.session
        self._recover(state)
        restore(session, state)
        response = state["messages"][-1]
        session.observe_reply(response)
        aggregate = session.stream_guard.aggregate
        if aggregate is not None and not session._complete_tool_arguments(aggregate):
            response = response.model_copy(update={
                "tool_calls": [], "invalid_tool_calls": [{"name": None, "args": "",
                    "id": None, "error": "incomplete_tool_arguments",
                    "type": "invalid_tool_call"}]})
        check_cancelled(session.config)
        if not response.invalid_tool_calls:
            extra = input_additions(session, state, response=response)
            if extra:
                session._round_index += 1
                return {**capture(session), "messages": extra, "jump_to": "model"}
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
        self._recover(request.state)
        return self._tool_result(request, handler)

    def _tool_result(self, request, handler):
        call = request.tool_call
        command = None
        session = self.session
        check_cancelled(session.config)
        if call["id"] in self.cached:
            message = self.cached[call["id"]]
        elif session.outcome is not None:
            message = ToolMessage(content="尚未执行，等待当前操作的确认。",
                                  tool_call_id=call["id"], artifact={"phase": "deferred"})
        else:
            before = len(self.exchange)
            command = append_result(session, request, handler, self.exchange,
                                    max(0, session._round_index - 1), self.attachments)
            if command is not None:
                adopt_command(session, command.update)
            execution = (session.outcome.execution or {}) if session.outcome else {}
            if execution.get("pending_confirm"):
                session.state["awaiting_call"] = call["id"]
                if call["id"] != self.calls[-1]["id"]:
                    session.outcome = pause_notice(session.outcome)
            if len(self.exchange) > before:
                message = self.exchange[-1]
            else:
                proposal = capture(session)["proposal"]
                message = ToolMessage(content=json.dumps({
                    "status": "awaiting_confirmation" if proposal else "terminal",
                    "result": (proposal["execution"]["pending_confirm"].get("q", proposal["reply"])
                               if proposal else session.outcome.reply)}, ensure_ascii=False),
                    name=call["name"], tool_call_id=call["id"], artifact={
                        "kind": "prepared" if proposal else "terminal",
                        "phase": "awaiting_approval" if proposal else "terminal",
                        "proposal": proposal})
                session.observations.extend(memory.observation(call, message))
        message = message.model_copy(update={"name": call["name"]})
        if self.calls and call["id"] == self.calls[-1]["id"]:
            update = {**capture(session), **(command.update if command else {}), "messages": [
                message, *stage(self.attachments, session.transient)]}
            return replace(command, update=update) if command else Command(update=update)
        if command is not None:
            return replace(command, update={**command.update, "messages": [message]})
        return message


class OrderedToolsMiddleware(AgentMiddleware):
    """Serialize the complete policy chain for each model's ordered tool batch."""

    def __init__(self, policy):
        self.policy = policy

    def wrap_tool_call(self, request, handler):
        policy = self.policy
        with policy._order:
            policy._recover(request.state)
            while (policy._next < len(policy.calls)
                   and policy.calls[policy._next]["id"] in policy.cached):
                policy._next += 1
            if request.tool_call["id"] in policy.cached:
                return handler(request)
            while (not policy._failed
                   and policy.calls[policy._next]["id"] != request.tool_call["id"]):
                check_cancelled(policy.session.config)
                policy._order.wait(0.05)
            if policy._failed:
                return ToolMessage(content="前序操作等待接续，本项尚未执行。" if policy._paused
                                   else "前序调用失败，本项未执行。",
                                   status="success" if policy._paused else "error",
                                   artifact={"phase": "deferred"} if policy._paused else None,
                                   tool_call_id=request.tool_call["id"])
            try:
                result = handler(request)
                messages = (result.update.get("messages", ())
                            if isinstance(result, Command) else [result])
                for message in messages:
                    if isinstance(message, ToolMessage):
                        policy.cached[message.tool_call_id] = message
                        policy.exchange = [message if isinstance(old, ToolMessage)
                            and old.tool_call_id == message.tool_call_id else old
                            for old in policy.exchange]
                return result
            except GraphInterrupt:
                policy._paused = policy._failed = True
                raise
            except BaseException:
                policy._failed = True
                raise
            finally:
                policy._next += 1
                policy._order.notify_all()
