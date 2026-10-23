"""Adapt the existing graph checkpoint to the native conversation store.

Graph state construction and node names stay here until the compatibility graph
is retired. Raw get/update methods remain for existing maintenance callers; they
are deliberately absent from the ConversationStore protocol.
"""

from agent.runtime import checkpoint_store, memory
from agent.runtime.conversation_store import ConversationSnapshot
from agent.runtime.turns import check_cancelled, conversation_turn
from utils.trace import record


class GraphConversationStore:
    def __init__(self, graph):
        self.graph = graph

    @property
    def saver(self):
        return getattr(self.graph, "checkpointer", None)

    @property
    def available(self) -> bool:
        return (
            self.saver is not None
            and callable(getattr(self.graph, "get_state", None))
            and callable(getattr(self.graph, "update_state", None))
        )

    def get_state(self, config, **kwargs):
        return self.graph.get_state(config, **kwargs)

    def _check_write(self, config):
        check_cancelled(config)
        thread_id = ((config or {}).get("configurable") or {}).get("thread_id")
        if self.saver is not None and thread_id:
            checkpoint_store.require_active(self.saver, thread_id)

    def update_state(self, config, values, **kwargs):
        self._check_write(config)
        return self.graph.update_state(config, values, **kwargs)

    def load(self, config) -> ConversationSnapshot:
        if not self.available:
            return ConversationSnapshot()
        snapshot = self.get_state(config)
        if snapshot is None:
            return ConversationSnapshot()
        values = snapshot.values or {}
        return ConversationSnapshot(
            messages=list(values.get("messages") or []),
            pending_confirm=values.get("pending_confirm"),
            pending_action=values.get("pending_action"),
            unfinished=bool(snapshot.next or values.get("done") is False),
        )

    def save_turn(
        self, config, messages, reply, *, observations=(), execution=None, pending=None
    ) -> None:
        if not self.available:
            return
        self._check_write(config)
        previous = self.load(config)
        principal = ((config or {}).get("configurable") or {}).get("principal")
        current = memory.turn_messages(messages, observations, reply, principal)
        from agent.graph import graph_input

        saved = graph_input(memory.checkpoint_messages(previous.messages, current))
        saved.update(pending or {})
        saved.update(execution or {})
        saved["done"] = True
        self.update_state(config, saved, as_node="gate")
        record("hybrid", "checkpoint_write", messages=len(saved["messages"]) - 1)

    def clear_pending(self, config, jti: str) -> None:
        self._check_write(config)
        if (self.load(config).pending_action or {}).get("jti") == jti:
            self.update_state(
                config, {"pending_confirm": None, "pending_action": None}, as_node="gate"
            )
            self.prune(config)

    def guard(self, config):
        return conversation_turn(self.saver, config)

    def is_resuming(self, config) -> bool:
        from agent.checkpoint import thread_is_resuming

        thread_id = ((config or {}).get("configurable") or {}).get("thread_id")
        return thread_is_resuming(self, thread_id)

    def prune(self, config) -> None:
        thread_id = ((config or {}).get("configurable") or {}).get("thread_id")
        if self.saver is not None and thread_id:
            checkpoint_store.prune(self.saver, thread_id)
