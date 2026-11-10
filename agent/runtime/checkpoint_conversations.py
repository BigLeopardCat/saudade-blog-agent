"""Conversation checkpoints through the framework's public state API.

The maintenance graph shares the approval schema and reducers with execution.
Explicit bound controls resume saved tasks; signed approval separately authorizes writes.
"""

from contextlib import contextmanager
from threading import local

from agent.runtime import checkpoint_store, memory
from agent.runtime.approval import graph
from agent.runtime.conversation_store import (
    ConversationAccessDenied,
    ConversationSnapshot,
    owned_config,
    require_owner,
)
from agent.runtime.turns import check_cancelled, conversation_turn

_guards = local()


class CheckpointConversationStore:
    def __init__(self, saver):
        self.saver = saver
        self._state = graph(saver)

    @property
    def available(self) -> bool:
        return self.saver is not None

    def get_state(self, config, **kwargs):
        config = owned_config(config)
        cfg = config["configurable"]
        if cfg.get("checkpoint_id") or cfg.get("checkpoint_ns"):
            # Historical and subgraph reads also require ownership of the current root.
            root = {"configurable": {"thread_id": cfg["thread_id"], "checkpoint_ns": ""}}
            saved = self.saver.get_tuple(root) if self.available else None
            require_owner(saved.metadata if saved else None, config)
        snapshot = self._state.get_state(config, **kwargs)
        require_owner(snapshot.metadata, config)
        return snapshot

    def update_state(self, config, values, **kwargs):
        check_cancelled(config)
        with self.guard(config) as config:
            if set(values) == {"messages"}:
                active = self.load(config)
                if active.unfinished and not active.approval:
                    raise ValueError("cannot_maintain_active_task")
            return self._state.update_state(config, values, as_node="commit", **kwargs)

    def load(self, config) -> ConversationSnapshot:
        if not self.available:
            return ConversationSnapshot()
        snapshot = self.get_state(config)
        values = snapshot.values or {}
        return ConversationSnapshot(
            messages=list(values.get("messages") or []),
            pending_confirm=values.get("pending_confirm"),
            pending_action=values.get("pending_action"),
            approval=values.get("approval"),
            run=values.get("run"),
            receipts=list(values.get("receipts") or []),
            last_outcome=values.get("last_outcome"),
            tool_domains=values.get("tool_domains"),
            unfinished=bool(values.get("run")),
            initialized=snapshot.created_at is not None,
        )

    def clear_pending(self, config, jti: str, *, observations=()) -> None:
        with self.guard(config) as config:
            previous = self.load(config)
            if (previous.pending_action or {}).get("jti") == jti:
                saved = {"pending_confirm": None, "pending_action": None,
                         "approval": None, "approved_result": None, "run": None}
                if observations:
                    saved["messages"] = memory.checkpoint_messages(previous.messages, observations)
                self.update_state(config, saved)
                self.prune(config)

    def replace_pending(self, config, jti, run, messages):
        with self.guard(config) as config:
            previous = self.load(config)
            if (previous.pending_action or {}).get("jti") != jti:
                raise ValueError("approval_changed")
            # Commit the already revoked grant and accepted input even if stop arrived
            # after revocation. This bounded checkpoint never dispatches tools or models.
            self._state.update_state(config, {"pending_confirm": None, "pending_action": None,
                "approval": None, "approved_result": None, "run": run,
                "receipts": [], "messages": messages}, as_node="commit")

    @contextmanager
    def guard(self, config, *, timeout=60):
        if not self.available:
            yield config
            return
        config = owned_config(config)
        thread = config["configurable"]["thread_id"]
        key = (id(self.saver), thread)
        held = getattr(_guards, "held", {})
        if key in held:
            if held[key] != config["configurable"]["user_id"]:
                raise ConversationAccessDenied("conversation_not_found")
            self.get_state(config)
            yield config
            return
        with conversation_turn(self.saver, config, timeout=timeout):
            self.get_state(config)
            # Maintenance inside a turn reuses its lock and PostgreSQL connection.
            _guards.held = {**held, key: config["configurable"]["user_id"]}
            try:
                yield config
            finally:
                _guards.held = held

    def is_resuming(self, config) -> bool:
        # This is a state probe; execution still requires an explicit bound control.
        return self.load(config).unfinished

    def prune(self, config) -> None:
        thread_id = ((config or {}).get("configurable") or {}).get("thread_id")
        if self.available and thread_id:
            self.get_state(config)
            checkpoint_store.prune(self.saver, thread_id)
