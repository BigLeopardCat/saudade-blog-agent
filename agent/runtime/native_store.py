"""Conversation checkpoints through the framework's public state API.

The maintenance graph shares the approval schema and reducers with execution.
Explicit bound controls resume saved tasks; signed approval separately authorizes writes.
"""

from agent.runtime import checkpoint_store, memory
from agent.runtime.approval import graph
from agent.runtime.conversation_store import ConversationSnapshot
from agent.runtime.turns import check_cancelled, conversation_turn


class NativeConversationStore:
    def __init__(self, saver):
        self.saver = saver
        self._state = graph(saver)

    @property
    def available(self) -> bool:
        return self.saver is not None

    def get_state(self, config, **kwargs):
        return self._state.get_state(config, **kwargs)

    def update_state(self, config, values, **kwargs):
        check_cancelled(config)
        thread_id = ((config or {}).get("configurable") or {}).get("thread_id")
        if thread_id:
            checkpoint_store.require_active(self.saver, thread_id)
        # Maintenance writes name their destination explicitly instead of inferring
        # a writer from checkpoint history.
        node = "commit"
        if set(values) == {"messages"}:
            active = self.load(config)
            if active.unfinished and not active.approval:
                raise ValueError("cannot_maintain_active_task")
        return self._state.update_state(config, values, as_node=node, **kwargs)

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
            unfinished=bool(values.get("run")),
            initialized=snapshot.created_at is not None,
        )

    def clear_pending(self, config, jti: str, *, observations=()) -> None:
        previous = self.load(config)
        if (previous.pending_action or {}).get("jti") == jti:
            saved = {"pending_confirm": None, "pending_action": None,
                     "approval": None, "approved_result": None, "run": None}
            if observations:
                saved["messages"] = memory.checkpoint_messages(previous.messages, observations)
            self.update_state(config, saved)
            self.prune(config)

    def guard(self, config):
        return conversation_turn(self.saver, config)

    def is_resuming(self, config) -> bool:
        # This is a state probe; execution still requires an explicit bound control.
        return self.load(config).unfinished

    def prune(self, config) -> None:
        thread_id = ((config or {}).get("configurable") or {}).get("thread_id")
        if self.available and thread_id:
            checkpoint_store.prune(self.saver, thread_id)
