"""Native model/tool turns with conversation persistence and no planner fallback."""

import asyncio
from contextlib import ExitStack
from dataclasses import replace
from functools import partial

from langchain_core.messages import AIMessageChunk
from openai import OpenAIError

from agent.runtime import approval, memory, recovery, tool_state, trace_context
from agent.runtime.results import TurnResult
from agent.runtime.session import ToolSession
from agent.runtime.task_inputs import InputConflict, TaskInputEvent, for_saver
from agent.runtime.turns import check_cancelled
from config.settings import settings
from models.llm import get_llm
from models.session import session_scope
from utils.trace import record


class NativeRuntime:
    def __init__(self, store, *, catalog=None, model_factory=None, narrator=None, application=None):
        self.store = store
        self.inputs = for_saver(store.saver)
        if application is None:
            from agent.products.blog.application import BlogApplication
            application = BlogApplication(catalog)
        elif catalog is not None:
            raise ValueError("Pass a catalog through the selected application")
        self.application = application
        self.model_factory = model_factory
        self.narrator = narrator

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
        with self.conversation_guard(config), session_scope(self.application.session_key(
                cfg.get("principal"), cfg.get("conversation_id"))):
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
        grant = state.get("confirm_grant")
        if grant is not None:
            yield from self._confirmed(state, config, snapshot, grant)
            return
        if state.get("resume_task"):
            yield from self._resume_task(snapshot, state["resume_task"], config)
            return
        if self.application.expired(snapshot.pending):
            snapshot = replace(snapshot, pending_confirm=None, pending_action=None,
                               approval=None)
        pending = (self.application.pending(snapshot.pending, config)
                   if self.store.available else None)
        principal = ((config or {}).get("configurable") or {}).get("principal")
        incoming = [*memory.bootstrap(
                        snapshot, state.get("messages") or [], principal,
                        tool_access=self.application.history_access),
                    *(state.get("messages") or [])]
        if (snapshot.unfinished and not snapshot.run.get("input_saved")
                and recovery.allowed(snapshot, snapshot.run["id"], config)):
            incoming = [*snapshot.run["input"], *incoming]
        prompt = self.application.prompt([*snapshot.messages, *incoming], principal, pending)
        session = self._session(config, snapshot, pending)
        tool_state.inherit(session, snapshot.tool_state, prompt)
        session.task = recovery.new_run(config, incoming, state.get("task_id"))
        if (self.inputs and snapshot.run and session.task
                and snapshot.run["id"] != session.task["id"]):
            try:
                self.inputs.finish(snapshot.run, set())
            except InputConflict as exc:
                # Tasks predating input support have no inbox row.
                if str(exc) != "task_not_available":
                    raise
        outcome = yield from self._run(session, prompt)
        execution = outcome.execution or {}
        if execution.get("client_action"):
            record("native", "task_paused", task_id=session.task["id"], reason="browser")
            yield outcome
            return
        approval_state = session.approval
        if not (execution.get("pending_confirm") or execution.get("pending_event")
                or execution.get("noop_note")):
            approval_state = snapshot.approval
        self.store.save_turn(
            config, [*incoming, *session.applied_messages], outcome.reply,
            observations=session.observations,
            execution=outcome.execution, pending=snapshot.pending, approval=approval_state,
        )
        record("native", "turn_completed", chars=len(outcome.reply))
        yield outcome

    def _resume_task(self, snapshot, task_id, config):
        if (snapshot.has_pending and recovery.allowed(replace(
                snapshot, pending_confirm=None, pending_action=None), task_id, config)
                and self.application.pending(snapshot.pending, config)):
            result = TurnResult("当前操作仍等待你的确认。", {
                **snapshot.pending, "task_id": task_id, "receipts": []})
            if not snapshot.run.get("input_saved"):
                self.store.save_turn(config, snapshot.run["input"], result.reply,
                                     execution=result.execution, approval=snapshot.approval)
            yield result
            return
        if not recovery.allowed(snapshot, task_id, config):
            record("native", "task_resume_rejected", task_id=task_id)
            yield TurnResult("当前任务正在等待确认，请先处理确认卡，没有恢复执行。"
                             if snapshot.has_pending else
                             "该任务已结束、被替换或当前身份已变化，没有恢复执行。",
                             {"blocked": True})
            return
        incoming = snapshot.run["input"]
        prompt = self.application.prompt([*snapshot.messages, *incoming],
                                        config["configurable"]["principal"])
        session = self._session(config, replace(snapshot, messages=[*snapshot.messages, *incoming]))
        session.task = snapshot.run
        session.receipts = list(snapshot.receipts)
        record("native", "task_resumed", task_id=task_id, engine="langgraph")
        from agent.runtime.client_actions import resume_value
        client = resume_value(self.get_state(config, subgraphs=True), task_id, config)
        if isinstance(client, TurnResult):
            yield client
            return
        outcome = yield from self._run(session, prompt, recover=True, client_result=client)
        if (outcome.execution or {}).get("client_action"):
            yield outcome
            return
        saved_input = ([] if snapshot.run.get("input_saved") else incoming)
        saved_input = [*saved_input, *session.applied_messages]
        self.store.save_turn(config, saved_input, outcome.reply,
                             observations=recovery.new_observations(snapshot, session.observations),
                             execution=outcome.execution, approval=session.approval)
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
        adapt = partial(pending.transition, config=config, saver=self.saver) if pending else None
        session = ToolSession(model, config, tools, domains, adapt, store=self.store,
                           isolated=snapshot.has_pending,
                           descriptions=self.application.descriptions,
                           middleware=self.application.middleware,
                           history_access=self.application.history_access)
        session.input_inbox = self.inputs
        return session

    @staticmethod
    def _run(session, prompt, *, resume=None, approval_input=(), recover=False, client_result=None):
        inbox = session.input_inbox
        session.inputs_active = bool(inbox and session.task and not session.isolated
                                     and resume is None and (recover or ((session.config or {}).get(
                                         "configurable") or {}).get("task_inputs")))
        session.input_events_enabled = bool(session.inputs_active and ((session.config or {}).get(
            "configurable") or {}).get("task_inputs"))
        if session.inputs_active:
            enabled = inbox.open(session.task, resume=recover)
            if session.input_events_enabled:
                yield TaskInputEvent({"task_id": session.task["id"], "ready": enabled})
        runner = session.stream(prompt, resume=resume, approval_input=approval_input,
                                recover=recover, client_result=client_result)
        outcome = None
        try:
            while True:
                try:
                    chunk = next(runner)
                except StopIteration as stop:
                    outcome = stop.value
                    break
                # The transport gets body deltas only. Provider reasoning, tool
                # argument fragments and usage metadata stay inside the session.
                yield (chunk if isinstance(chunk, TaskInputEvent)
                       else AIMessageChunk(content=chunk.content))
        finally:
            runner.close()
            if session.inputs_active:
                items = inbox.finish(session.task, session.applied_inputs,
                    paused=outcome is None or bool((outcome.execution or {}).get("client_action")))
        if session.input_events_enabled:
            yield TaskInputEvent({"task_id": session.task["id"], "ready": False,
                                  "inputs": items})
        if not isinstance(outcome, TurnResult):
            raise RuntimeError("Native tool session ended without a terminal result")
        return outcome

    def _confirmed(self, state, config, snapshot, grant):
        domain = self.application.confirmation(grant)
        if not self.store.available:
            raise RuntimeError("操作确认需要持久化服务，本次未执行。")
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
        outcome = domain.execute(grant, config, self.saver)
        execution = {key: value for key, value in outcome.items() if key != "reply"}
        matching = (snapshot.pending_action or {}).get("jti") == grant.get("jti")
        retained = None if matching and execution.get("receipts") else snapshot.pending
        result = TurnResult(outcome["reply"], execution)
        modern = bool(matching and execution.get("receipts")
                      and approval.matches(snapshot.approval, grant, config))
        if modern:
            emit(config, "操作已完成，正在继续处理剩余请求…", key="confirmation_resume")
            record("native", "confirmation_resumed", engine="langgraph")
            clean = replace(snapshot, pending_confirm=None, pending_action=None, approval=None)
            session = self._session(config, clean)
            session.task = snapshot.run
            session.receipts = list(execution["receipts"])
            session.effects = {key: value for key, value in execution.items() if key != "receipts"}
            prompt = self.application.prompt([], session.principal)
            facts = {"jti": grant["jti"], "reply": result.reply, "execution": execution}
            result = yield from self._continue_session(session, prompt, facts,
                                                       state.get("messages") or [], result)
            self.store.save_turn(
                config, [], result.reply, observations=session.observations,
                execution=result.execution, approval=session.approval,
            )
            yield result
            return
        observations = []
        if execution.get("receipts"):
            from agent.runtime.results import receipt_observations

            observations = receipt_observations(result)
            if self.narrator is not None:
                result = TurnResult(self.narrator(result, config), execution)
        self.store.save_turn(
            config, state.get("messages") or [], result.reply, execution=execution,
            pending=retained, observations=observations,
            approval=None if matching and execution.get("receipts") else snapshot.approval,
        )
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
                visible.append(chunk.content)
                yield chunk
        except (OpenAIError, TimeoutError) as error:
            record("native", "task_stopped", reason="continuation_failed",
                   error=type(error).__name__)
            suffix = (("\n" if visible else "") + executed.reply
                      + "\n后续处理暂时中断，请继续指定要处理的事项。")
            yield AIMessageChunk(content=suffix)
            return TurnResult("".join(visible) + suffix, {"receipts": session.receipts})
        finally:
            runner.close()


def build(*, resources: ExitStack) -> NativeRuntime:
    from agent import checkpoint
    from agent.products.blog.application import BlogApplication
    from agent.products.blog.narration import narrate
    from agent.runtime.native_store import NativeConversationStore

    return NativeRuntime(
        NativeConversationStore(checkpoint.build_checkpointer(resources=resources)),
        narrator=narrate, application=BlogApplication(),
    )
