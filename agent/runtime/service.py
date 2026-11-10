"""LangChain model/tool execution with durable conversation and approval state."""

import asyncio
import uuid
from dataclasses import replace
from functools import partial

from langchain_core.messages import AIMessageChunk
from openai import OpenAIError

from agent.runtime import approval, checkpoint_store, memory, recovery, trace_context
from agent.runtime.results import ReplyReset, TurnResult
from agent.runtime.session import ToolSession
from agent.runtime.task_inputs import InputConflict, TaskInputEvent, for_saver
from agent.runtime.turns import check_cancelled
from config.settings import settings
from models.llm import get_llm
from models.session import session_scope
from models.transport import io_scope
from utils.trace import record


class AgentRuntime:
    def __init__(self, store, *, application, model_factory=None, model_io=None):
        self.store = store
        self.inputs = for_saver(store.saver)
        self.application = application
        self.model_factory = model_factory
        self.model_io = model_io

    @property
    def saver(self):
        return self.store.saver

    def get_state(self, config, **kwargs):
        return self.store.get_state(config, **kwargs)

    def update_state(self, config, values, **kwargs):
        return self.store.update_state(config, values, **kwargs)

    async def aget_state(self, config, **kwargs):
        return await asyncio.to_thread(self.get_state, config, **kwargs)

    async def aupdate_state(self, config, values, **kwargs):
        return await asyncio.to_thread(self.update_state, config, values, **kwargs)

    def conversation_guard(self, config):
        return self.store.guard(config)

    def stream_events(self, state, config=None):
        """Yield visible deltas, then exactly one persisted canonical TurnResult.

        The final reply is not another delta. Confirmation text travels in the
        existing execution fields; model reasoning and call arguments stay private.
        Cancellation, storage failure and generator close produce no final result.
        """
        cfg = ((config or {}).get("configurable") or {})
        with (io_scope(self.model_io, cancel_check=partial(check_cancelled, config)),
              self.conversation_guard(config) as config,
              session_scope(self.application.session_key(
                  cfg.get("principal"), cfg.get("conversation_id")))):
            try:
                trace_context.caller(self.application.context(cfg.get("principal")))
                for event in self._turn(state, config):
                    if isinstance(event, TurnResult):
                        trace_context.outcome(event)
                    yield event
            finally:
                self.store.prune(config)

    def _turn(self, state, config):
        check_cancelled(config)
        snapshot = self.store.load(config)
        if self._discarded_turn(state, config, snapshot):
            yield TurnResult("该轮次已丢弃，请重新发送新的请求。",
                             {"blocked": [{"reason": "turn_discarded"}]})
            return
        if state.get("system_event"):
            yield from self._system_event(state, snapshot, config)
            return
        grant = state.get("confirm_grant")
        if grant is not None:
            yield from self._confirmed(state, config, snapshot, grant)
            return
        if state.get("resume_task"):
            yield from self._resume_task(snapshot, state["resume_task"], config)
            return
        pending = (self.application.pending(snapshot.pending, config)
                   if self.store.available else None)
        if snapshot.has_pending and (self.application.expired(snapshot.pending) or not pending
                or not snapshot.run
                or not approval.matches(snapshot.approval, pending.grant, config)):
            self.store.clear_pending(config, (snapshot.pending_action or {}).get("jti"))
            snapshot, pending = self.store.load(config), None
        principal = ((config or {}).get("configurable") or {}).get("principal")
        incoming = [*memory.bootstrap(
                        snapshot, state.get("messages") or [], principal,
                        tool_access=self.application.history_access),
                    *memory.request_messages(state.get("messages") or [])]
        task_id = state.get("task_id") or uuid.uuid4().hex
        incoming = memory.identify_turn(incoming, state.get("turn_id") or task_id)
        if pending:
            yield from self._replace_pending(snapshot, pending, incoming, config, task_id)
            return
        yield from self._new_turn(snapshot, incoming, config, task_id)

    def _new_turn(self, snapshot, incoming, config, task_id, *, effects=None, event_seed=None):
        principal = ((config or {}).get("configurable") or {}).get("principal")
        incoming = [*recovery.input_context(snapshot, config), *incoming]
        prompt = self.application.prompt([*snapshot.messages, *incoming], principal)
        session = self._session(config, snapshot, None)
        session.state["effects"].update(effects or {})
        session.event_seed = event_seed
        session.turn_input = incoming
        session.task = recovery.new_run(config, incoming, task_id)
        if (self.inputs and snapshot.run and session.task
                and snapshot.run["id"] != session.task["id"]):
            try:
                self.inputs.finish(snapshot.run, set())
            except InputConflict as exc:
                if str(exc) != "task_not_available":
                    raise
        outcome = yield from self._run(session, prompt)
        execution = outcome.execution or {}
        if execution.get("client_action") or execution.get("paused_task"):
            record("native", "task_paused", task_id=session.task["id"],
                   reason="requested" if execution.get("paused_task") else "browser")
            yield outcome
            return
        record("native", "turn_completed", chars=len(outcome.reply))
        yield outcome

    def _system_event(self, state, snapshot, config):
        plan = self.application.system_event(state["system_event"], snapshot, config)
        if isinstance(plan, TurnResult):
            if plan.reply:
                yield AIMessageChunk(content=plan.reply)
            yield plan
            return
        config = {**config, "configurable": {**config["configurable"],
                  "system_event_id": state["task_id"]}}
        if snapshot.unfinished:
            if snapshot.run["id"] != state["task_id"] or snapshot.has_pending:
                raise ValueError("image_event_deferred")
            yield from self._resume_task(snapshot, state["task_id"], config)
            return
        yield from self._new_turn(snapshot, [], config, state["task_id"], event_seed=plan)

    def _discarded_turn(self, state, config, snapshot):
        turn_id = state.get("turn_id") or state.get("task_id")
        candidates = [turn_id]
        if snapshot.run:
            candidates.extend([memory.latest_turn_id(snapshot.messages),
                               snapshot.run.get("turn_id"),
                               memory.latest_turn_id(snapshot.run.get("input", []))])
        thread_id = ((config or {}).get("configurable") or {}).get("thread_id")
        return checkpoint_store.turns_deleted(self.saver, thread_id, candidates)

    def _replace_pending(self, snapshot, pending, incoming, config, task_id):
        from agent.runtime.results import cancellation_observations

        revoked = pending.feedback(config, self.saver)
        if self.inputs:
            self.inputs.finish(snapshot.run, set())
        self.store.clear_pending(config, pending.grant["jti"], observations=(
            cancellation_observations("用户提出了新要求，原待确认操作已撤销，未执行改动。")))
        previous = snapshot.run["id"]
        snapshot = self.store.load(config)
        record("native", "task_replaced", prior_task_id=previous, task_id=task_id)
        yield from self._new_turn(snapshot, incoming, config, task_id, effects=revoked.execution)

    def _resume_task(self, snapshot, task_id, config):
        resumable = (replace(snapshot, pending_confirm=None, pending_action=None)
                     if snapshot.has_pending else snapshot)
        if not recovery.allowed(resumable, task_id, config):
            record("native", "task_resume_rejected", task_id=task_id)
            yield TurnResult("当前任务正在等待确认，请先处理确认卡，没有恢复执行。"
                             if snapshot.has_pending else
                             "该任务已结束、被替换或当前身份已变化，没有恢复执行。",
                             {"blocked": True})
            return
        incoming = snapshot.run["input"]
        prompt = self.application.prompt([*snapshot.messages, *incoming],
                                        config["configurable"]["principal"])
        session = self._session(config, replace(snapshot, messages=[*snapshot.messages, *incoming]),
                                self.application.pending(snapshot.pending, config))
        session.task = snapshot.run
        session.receipts = list(snapshot.receipts)
        record("native", "task_resumed", task_id=task_id, engine="langgraph")
        outcome = yield from self._run(session, prompt, recover=True)
        yield outcome

    def _session(self, config, snapshot, pending=None):
        tools, domains = self.application.tools(
            config, snapshot, pending, self.saver, self.store.available)
        effort = settings.agent_reasoning_effort
        options = {"reasoning_effort": effort} if effort != "none" else {}
        model = (self.model_factory or get_llm)(
            enable_thinking=effort != "none", streaming=True, stream_usage=True,
            max_tokens=settings.agent_output_tokens, **options
        )
        session = ToolSession(model, config, tools, domains, store=self.store,
                           descriptions=self.application.descriptions,
                           middleware=self.application.middleware,
                           history_access=self.application.history_access)
        from agent.runtime.conversation_store import bound_tool_domains

        session.state["tool_domains"] = bound_tool_domains(snapshot.tool_domains, config)
        from agent.runtime.tool_state import apply

        apply(session, session.state["tool_domains"])
        session.input_inbox = self.inputs
        session.feedback_handler = partial(pending.feedback, saver=self.saver) if pending else None
        return session

    @staticmethod
    def _run(session, prompt, *, resume=None, approval_input=(), recover=False):
        if session.task:
            source = memory.latest_turn_id(session.turn_input)
            session.task = {**session.task, "turn_id":
                session.config.get("configurable", {}).get("turn_id") or source
                or session.task.get("turn_id") or session.task["id"]}
        if session.task and session.store and session.store.available:
            from agent.runtime.task_budget import for_saver as task_usage
            session.budget.bind_task(task_usage(session.store.saver), session.task,
                                    create=not (recover or resume is not None))
        inbox = session.input_inbox
        session.inputs_active = bool(inbox and session.task
                                     and not (recover and session.feedback_handler)
                                     and resume is None)
        session.input_events_enabled = bool(session.inputs_active and ((session.config or {}).get(
            "configurable") or {}).get("task_inputs"))
        if session.inputs_active:
            try:
                enabled = inbox.open(session.task, resume=recover)
            except InputConflict:
                if not recover:
                    raise
                record("native", "task_resume_rejected", task_id=session.task["id"],
                       reason="task_input_unavailable")
                return TurnResult("任务的补充指令记录不可用，未恢复执行，请重新发起任务。",
                                  {"blocked": [{"reason": "task_input_unavailable"}]})
            if session.input_events_enabled:
                yield TaskInputEvent({"task_id": session.task["id"], "ready": enabled})
        runner = session.stream(prompt, resume=resume, approval_input=approval_input,
                                recover=recover)
        outcome = None
        failure = None
        try:
            while True:
                try:
                    chunk = next(runner)
                except StopIteration as stop:
                    outcome = stop.value
                    break
                # The transport gets body deltas only. Provider reasoning, tool
                # argument fragments and usage metadata stay inside the session.
                yield (chunk if isinstance(chunk, (TaskInputEvent, ReplyReset))
                       else AIMessageChunk(content=chunk.content))
        except (OpenAIError, TimeoutError) as error:
            failure = error
        finally:
            runner.close()
            items = _finish_inputs(session, outcome)
        completion = _input_completion(session, outcome, failure, items)
        if completion is not None:
            yield completion
        if failure is not None:
            raise failure
        if not isinstance(outcome, TurnResult):
            raise RuntimeError("Native tool session ended without a terminal result")
        return outcome

    def _confirmed(self, state, config, snapshot, grant):
        domain = self.application.confirmation(grant)
        if not self.store.available:
            raise RuntimeError("操作确认需要持久化服务，本次未执行。")
        if snapshot.run and approval.matches(snapshot.approval, grant, config):
            from agent.runtime.budget import current_budget
            from agent.runtime.task_budget import for_saver as task_usage
            budget = current_budget()
            if budget is not None:
                budget.bind_task(task_usage(self.saver), snapshot.run, create=False)
        from agent.runtime.progress import emit

        emit(
            config,
            "正在执行已确认的工具："
            + ", ".join(
                spec["tool"]
                for spec in grant.get("specs", ())
                if isinstance(spec, dict) and isinstance(spec.get("tool"), str)
            ),
            key="confirmed_execution",
        )
        matching = ((snapshot.pending_action or {}).get("jti") == grant.get("jti")
                    and approval.matches(snapshot.approval, grant, config))
        execution_config = (config if matching else {**config, "configurable": {
            **config["configurable"], "confirmation_replay_only": True}})
        outcome = domain.execute(grant, execution_config, self.saver)
        execution = {key: value for key, value in outcome.items() if key != "reply"}
        if grant.get("selection"):
            execution["approval_selection"] = grant["selection"]
        result = TurnResult(outcome["reply"], execution)
        modern = bool(matching and approval.has_result(execution, grant["specs"], jti=grant["jti"]))
        if modern:
            emit(config, "正在核对操作结果并继续处理请求…", key="confirmation_resume")
            record("native", "confirmation_resumed", engine="langgraph")
            clean = replace(snapshot, pending_confirm=None, pending_action=None, approval=None)
            session = self._session(config, clean)
            session.task = snapshot.run
            session.receipts = [*snapshot.receipts, *execution["receipts"]]
            prompt = self.application.prompt([], session.principal)
            facts = {"jti": grant["jti"], "reply": result.reply, "execution": execution}
            result = yield from self._continue_session(session, prompt, facts,
                                                       state.get("messages") or [], result)
            yield result
            return
        if result.reply:
            yield AIMessageChunk(content=result.reply)
        yield result

    def _continue_session(self, session, prompt, facts, incoming, executed):
        runner = self._run(session, prompt, resume=facts, approval_input=incoming)
        visible = []
        try:
            while True:
                try:
                    chunk = next(runner)
                except StopIteration as stop:
                    return stop.value
                if isinstance(chunk, ReplyReset):
                    visible.clear()
                elif isinstance(chunk, AIMessageChunk):
                    visible.append(chunk.content)
                yield chunk
        except (OpenAIError, TimeoutError) as error:
            record("native", "task_stopped", reason="continuation_failed",
                   error=type(error).__name__)
            suffix = (("\n" if visible else "") + executed.reply
                      + "\n后续处理暂时中断，请继续指定要处理的事项。")
            yield AIMessageChunk(content=suffix)
            return TurnResult("".join(visible) + suffix,
                              {**(executed.execution or {}), "receipts": session.receipts})
        finally:
            runner.close()


def _input_completion(session, outcome, failure, items):
    if not session.input_events_enabled:
        return None
    paused = bool(outcome and (outcome.execution or {}).get("paused_task"))
    if failure is not None:
        saved = session.store.load(session.config)
        paused = recovery.allowed(saved, session.task["id"], session.config)
    return TaskInputEvent({"task_id": session.task["id"], "ready": False, "inputs": items,
                           **({"phase": "paused"} if paused else {})})


def _finish_inputs(session, outcome):
    if not session.input_inbox or not session.task:
        return []
    execution = (outcome.execution or {}) if outcome else {}
    try:
        return session.input_inbox.finish(session.task, session.state["applied_inputs"],
            paused=outcome is None or bool(execution.get("paused_task")
                                          or execution.get("client_action")
                                          or execution.get("pending_confirm")),
            awaiting_confirmation=bool(execution.get("pending_confirm")))
    except InputConflict as exc:
        if str(exc) != "task_not_available":
            raise
        record("native", "task_inputs_missing", task_id=session.task["id"])
        return []
