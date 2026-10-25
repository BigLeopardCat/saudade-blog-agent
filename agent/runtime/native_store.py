"""Conversation checkpoints through the framework's public state API.

The single commit node supplies message reducers and checkpoint versioning. It is
never invoked as an agent and cannot execute nodes from an older checkpoint.
"""

from typing import Annotated, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages

from agent.runtime import checkpoint_store, memory
from agent.runtime.conversation_store import ConversationSnapshot
from agent.runtime.turns import check_cancelled, conversation_turn
from utils.trace import record


class ConversationState(TypedDict, total=False):
    messages: Annotated[list, add_messages]
    pending_confirm: dict | None
    pending_action: dict | None
    receipts: list


def _commit(state):
    return {}


class NativeConversationStore:
    def __init__(self, saver):
        self.saver = saver
        graph = StateGraph(ConversationState)
        graph.add_node("commit", _commit)
        graph.add_edge(START, "commit")
        graph.add_edge("commit", END)
        self._state = graph.compile(checkpointer=saver)

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
        # Maintenance writes also use this node; inference from a legacy writer
        # would otherwise try to resume an old planner or fail as ambiguous.
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
            initialized=snapshot.created_at is not None,
        )

    def save_turn(
        self, config, messages, reply, *, observations=(), execution=None, pending=None
    ) -> None:
        if not self.available:
            return
        previous = self.load(config)
        principal = ((config or {}).get("configurable") or {}).get("principal")
        current = memory.turn_messages(messages, observations, reply, principal)
        saved = {"messages": memory.checkpoint_messages(previous.messages, current),
                 "pending_confirm": None, "pending_action": None}
        for values in (pending or {}, execution or {}):
            saved.update({key: values[key] for key in
                          ("pending_confirm", "pending_action", "receipts") if key in values})
        self.update_state(config, saved)
        record("native", "checkpoint_write", messages=len(current))

    def clear_pending(self, config, jti: str) -> None:
        if (self.load(config).pending_action or {}).get("jti") == jti:
            self.update_state(config, {"pending_confirm": None, "pending_action": None})
            self.prune(config)

    def guard(self, config):
        return conversation_turn(self.saver, config)

    def is_resuming(self, config) -> bool:
        # Reads restart from the last complete turn. Confirmed writes use their
        # independent durable execution ledger, never a graph continuation.
        return False

    def prune(self, config) -> None:
        thread_id = ((config or {}).get("configurable") or {}).get("thread_id")
        if self.available and thread_id:
            checkpoint_store.prune(self.saver, thread_id)
