"""Native model/tool turns with conversation persistence and no planner fallback."""

import asyncio
from contextlib import ExitStack
from dataclasses import replace
from functools import partial

from langchain_core.messages import AIMessageChunk
from openai import OpenAIError

from agent.domains import conversation_images
from agent.domains.registry import confirmed_domain, native_domain
from agent.principal import CHAT_ONLY_ROLES
from agent.runtime import approval, memory, trace_context
from agent.runtime.capabilities import ToolCatalog
from agent.runtime.context import conversation_session
from agent.runtime.pending import expired
from agent.runtime.pending import restore as restore_pending
from agent.runtime.prompt import build_prompt
from agent.runtime.read_tools import own_reads_enabled
from agent.runtime.results import TurnResult
from agent.runtime.session import ToolSession
from agent.runtime.turns import check_cancelled
from config.settings import settings
from models.llm import get_llm
from models.session import session_scope
from utils.trace import record


class NativeRuntime:
    def __init__(self, store, *, catalog=None, model_factory=None, narrator=None):
        self.store = store
        self.catalog = catalog if catalog is not None else ToolCatalog()
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
        with self.conversation_guard(config), session_scope(conversation_session(
                cfg.get("principal"), cfg.get("conversation_id"))):
            try:
                trace_context.caller(cfg.get("principal"))
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
        if expired(snapshot.pending):
            snapshot = replace(snapshot, pending_confirm=None, pending_action=None,
                               approval=None)
        pending = restore_pending(snapshot.pending, config) if self.store.available else None
        principal = ((config or {}).get("configurable") or {}).get("principal")
        incoming = [*memory.bootstrap(snapshot, state.get("messages") or [], principal),
                    *(state.get("messages") or [])]
        own_enabled = own_reads_enabled(principal)
        prompt = build_prompt(
            [*snapshot.messages, *incoming], principal, own_enabled, pending
        )
        session = self._session(config, snapshot, pending)
        outcome = yield from self._run(session, prompt)
        execution = outcome.execution or {}
        approval_state = session.approval
        if not (execution.get("pending_confirm") or execution.get("pending_event")
                or execution.get("noop_note")):
            approval_state = snapshot.approval
        self.store.save_turn(
            config, incoming, outcome.reply, observations=session.observations,
            execution=outcome.execution, pending=snapshot.pending, approval=approval_state,
        )
        record("native", "turn_completed", chars=len(outcome.reply))
        yield outcome

    def _session(self, config, snapshot, pending=None):
        principal = ((config or {}).get("configurable") or {}).get("principal")
        own_enabled = own_reads_enabled(principal)
        # Unrestorable old cards remain stored, but cannot authorize a write or
        # be silently replaced during a rolling migration.
        can_prepare = self.store.available and (not snapshot.has_pending or pending is not None)
        chat_only = getattr(principal, "known_role", None) in CHAT_ONLY_ROLES
        tools = {} if chat_only else self.catalog.initial_tools(own_enabled)
        domains = (
            {} if chat_only else self.catalog.domains(config, can_prepare, pending, self.saver)
        )
        image_tools = conversation_images.tools(
            memory.history(snapshot.messages, principal), config)
        if image_tools and not chat_only:
            domains = {**domains, "conversation_images": image_tools}
        model = (self.model_factory or get_llm)(
            enable_thinking=False, streaming=True, stream_usage=True,
            max_tokens=settings.agent_output_tokens
        )
        adapt = partial(pending.transition, config=config, saver=self.saver) if pending else None
        return ToolSession(model, config, tools, domains, adapt, store=self.store,
                           isolated=snapshot.has_pending)

    @staticmethod
    def _run(session, prompt, *, resume=None, approval_input=()):
        runner = session.stream(prompt, resume=resume, approval_input=approval_input)
        try:
            while True:
                try:
                    chunk = next(runner)
                except StopIteration as stop:
                    outcome = stop.value
                    break
                # The transport gets body deltas only. Provider reasoning, tool
                # argument fragments and usage metadata stay inside the session.
                yield AIMessageChunk(content=chunk.content)
        finally:
            runner.close()
        if not isinstance(outcome, TurnResult):
            raise RuntimeError("Native tool session ended without a terminal result")
        return outcome

    def _confirmed(self, state, config, snapshot, grant):
        domain = confirmed_domain(grant)
        if domain is None or native_domain(grant) is not domain:
            raise ValueError("这张确认卡不支持当前执行入口，请重新发起操作；本次未执行。")
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
            session.receipts = list(execution["receipts"])
            session.effects = {key: value for key, value in execution.items() if key != "receipts"}
            prompt = build_prompt([], session.principal, own_reads_enabled(session.principal))
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
            from agent.runtime.narration import receipt_observations

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
    from agent.runtime.narration import narrate
    from agent.runtime.native_store import NativeConversationStore

    return NativeRuntime(
        NativeConversationStore(checkpoint.build_checkpointer(resources=resources)),
        narrator=narrate,
    )
