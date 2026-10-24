"""Native tool turns using conversation storage and a legacy execution fallback.

Completed turns cross the ConversationStore boundary. The existing graph remains
for compatibility execution and maintenance APIs while its planner is retired.
"""

import asyncio
from functools import partial

from langchain_core.messages import (
    AIMessage,
    AIMessageChunk,
)

from agent.domains import browser
from agent.domains.favorites import preparation as favorite_preparation
from agent.domains.messages import preparation as message_preparation
from agent.domains.notifications import preparation as notification_preparation
from agent.domains.registry import confirmed_domain
from agent.principal import CHAT_ONLY_ROLES
from agent.runtime.admin_domains import domains as admin_domains
from agent.runtime.conversation_store import ConversationStore
from agent.runtime.graph_store import GraphConversationStore
from agent.runtime.pending import restore as restore_pending
from agent.runtime.prompt import build_prompt as _input
from agent.runtime.read_tools import (
    admin_tools,
    article_tools,
    catalog_tools,
    device_domain,
    moderation_tools,
    own_domains,
    own_reads_enabled,
    own_tools,
    public_domains,
    queue_tools,
    site_tools,
)
from agent.runtime.session import MAX_PROMPT_CHARS, ToolSession
from agent.runtime.turns import check_cancelled
from agent.tasks import task_rows
from models.llm import get_llm
from utils.trace import record


class HybridArm:
    def __init__(self, legacy, read_tools: dict | None = None,
                 site_read_tools: dict | None = None,
                 own_read_tools: dict | None = None,
                 admin_read_tools: dict | None = None,
                 moderation_read_tools: dict | None = None,
                 queue_read_tools: dict | None = None, *,
                 store: ConversationStore | None = None) -> None:
        self.legacy = legacy
        self._checkpoint_api = GraphConversationStore(legacy)
        self.store = self._checkpoint_api if store is None else store
        self.read_tools = article_tools() if read_tools is None else read_tools
        self.site_read_tools = site_tools() if site_read_tools is None else site_read_tools
        self.own_read_tools = own_tools() if own_read_tools is None else own_read_tools
        self.admin_read_tools = admin_tools() if admin_read_tools is None else admin_read_tools
        self.moderation_read_tools = (moderation_tools() if moderation_read_tools is None
                                      else moderation_read_tools)
        self.catalog_read_tools = catalog_tools()
        self.queue_read_tools = queue_tools() if queue_read_tools is None else queue_read_tools
        # Keep the graph alias for existing compatibility probes and maintenance callers.
        self.graph = legacy

    @property
    def saver(self):
        return self.store.saver

    def get_state(self, config, **kwargs):
        return self._checkpoint_api.get_state(config, **kwargs)

    def update_state(self, config, values, **kwargs):
        return self._checkpoint_api.update_state(config, values, **kwargs)

    def conversation_guard(self, config):
        return self.store.guard(config)

    async def aget_state(self, config, **kwargs):
        # Production uses the synchronous PostgresSaver. Its async methods are
        # unsupported, so offload the sync graph API instead of forwarding them.
        return await asyncio.to_thread(self.get_state, config, **kwargs)

    async def aupdate_state(self, config, values, **kwargs):
        return await asyncio.to_thread(self.update_state, config, values, **kwargs)

    def stream(self, state: dict, config: dict | None = None, *, stream_mode=None,
               **kwargs):
        with self.conversation_guard(config):
            cfg = (config or {}).get("configurable") or {}
            if "idem_resuming" in cfg:
                config = {**(config or {}), "configurable": {
                    **cfg, "idem_resuming": self.store.is_resuming(config)}}
            try:
                yield from self._stream(state, config, stream_mode=stream_mode, **kwargs)
            finally:
                self.store.prune(config)

    def _stream(self, state, config, *, stream_mode=None, **kwargs):
        check_cancelled(config)
        cfg = (config or {}).get("configurable") or {}
        principal = cfg.get("principal")
        role = getattr(principal, "known_role", None)
        ledger = state.get("ledger") or {}
        saver = self.store.saver
        checkpointed = saver is not None
        can_bridge = self.store.available
        snapshot = self.store.load(config)
        pending = restore_pending(snapshot.pending, config) if can_bridge else None
        domain = confirmed_domain(state.get("confirm_grant"))
        if can_bridge and domain:
            outcome = domain.execute(state["confirm_grant"], config, saver)
            retained = snapshot.pending if (pending and (
                pending.grant["jti"] != state["confirm_grant"].get("jti"))) else None
            yield from self._finish(state, config, outcome.pop("reply"), outcome, retained)
            return
        if ((checkpointed and not can_bridge)
                or state.get("confirm_grant") or task_rows(cfg.get("open_tasks"))
                or (ledger.get("pending") and pending is None)
                or role in CHAT_ONLY_ROLES):
            record("hybrid", "legacy_handoff", reason="stateful")
            yield from self.legacy.stream(state, config, stream_mode=stream_mode,
                                          **kwargs)
            return

        if snapshot.unfinished or (pending is None and snapshot.has_pending):
            record("hybrid", "legacy_handoff", reason="checkpoint_state")
            yield from self.legacy.stream(state, config, stream_mode=stream_mode,
                                          **kwargs)
            return

        own_enabled = own_reads_enabled(principal)
        domains = self._domains(config, can_bridge, pending)

        history = snapshot.messages
        prompt = _input([*history, *(state.get("messages") or [])],
                        principal, own_enabled, pending)
        initial_chars = sum(len(str(message.content)) for message in prompt)
        if initial_chars > MAX_PROMPT_CHARS:
            record("hybrid", "legacy_handoff", reason="prompt_budget",
                   prompt_chars=initial_chars)
            yield from self.legacy.stream(state, config, stream_mode=stream_mode,
                                          **kwargs)
            return
        yield from self._native_stream(
            state, config, prompt, own_enabled, domains, pending, snapshot, saver,
            stream_mode=stream_mode, **kwargs)

    def _native_stream(self, state, config, prompt, own_enabled, domains, pending, snapshot, saver,
                       *, stream_mode=None, **kwargs):
        """Stream native model chunks, then persist one completed visible reply."""
        model = get_llm(enable_thinking=False, streaming=True, stream_usage=True, max_tokens=768)
        adapt = partial(pending.transition, config=config, saver=saver) if pending else None
        session = ToolSession(model, config, self._initial_tools(own_enabled), domains, adapt)
        streamed_reply = False
        native_stream = session.stream(prompt)
        try:
            while True:
                try:
                    chunk = next(native_stream)
                except StopIteration as stop:
                    outcome = stop.value
                    break
                if session.used_stream_api:
                    streamed_reply |= bool(str(getattr(chunk, "content", "") or "").strip())
                if session.used_stream_api and getattr(chunk, "content", None):
                    yield "messages", (chunk, {"langgraph_node": "model"})
        finally:
            native_stream.close()
        if outcome is None:
            yield from self.legacy.stream(state, config, stream_mode=stream_mode, **kwargs)
            return
        retained = snapshot.pending if pending else None
        yield from self._finish(state, config, outcome.reply, outcome.execution, retained,
                                session.observations, streamed_reply=streamed_reply)

    def _initial_tools(self, own_enabled):
        tools = dict(self.read_tools)
        if own_enabled and "get_unread_summary" in self.own_read_tools:
            tools["get_unread_summary"] = self.own_read_tools["get_unread_summary"]
        return tools

    def _domains(self, config, can_bridge, pending):
        cfg = (config or {}).get("configurable") or {}
        domains = {"site": {**self.site_read_tools, **browser.tools(config)},
                   **public_domains(), **device_domain(config)}
        domains.update(admin_domains(config, can_bridge, {
            "admin_reports": self.admin_read_tools,
            "admin_moderation": self.moderation_read_tools,
            "admin_catalog": self.catalog_read_tools,
            "admin_work_queue": self.queue_read_tools,
        }))
        if own_reads_enabled(cfg.get("principal")):
            domains.update(own_domains(self.own_read_tools))
        if can_bridge and favorite_preparation.available(config):
            domains["favorites"] = favorite_preparation.tools(self.own_read_tools)
            domains["notifications"] = notification_preparation.tools(self.own_read_tools)
            domains["messages"] = message_preparation.tools(self.own_read_tools)
        if pending:
            cancel = pending.cancel_tool(self.store.saver)
            domains["pending"] = {cancel.name: cancel}
        return domains

    def _finish(self, state, config, reply, execution=None, retained=None, observations=(),
                *, streamed_reply=False):
        self.store.save_turn(config, state.get("messages") or [], reply,
                             observations=observations, execution=execution, pending=retained)
        if execution is not None:
            yield "updates", {"execute": execution}
        record("hybrid", "direct_reply", chars=len(reply))
        terminal_card = execution and (execution.get("pending_confirm") or
                                       execution.get("noop_note"))
        if not streamed_reply and not terminal_card:
            yield "messages", (AIMessageChunk(content=reply), {"langgraph_node": "model"})
        yield "updates", {"model": {"messages": [AIMessage(content=reply)]}}
        yield "updates", {"gate": {"done": True}}


def build() -> HybridArm:
    from agent import checkpoint
    from agent.agent import create_agent
    return HybridArm(create_agent(checkpointer=checkpoint.build_checkpointer()))
