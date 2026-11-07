"""Conversation data and the storage boundary used by the Agent runtime.

Conversation turns, task identities and framework approval bindings are saved.
Reads without a committed checkpoint may run again; business writes remain owned
by the independent execution ledger.
"""

from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from typing import Any, Protocol, TypedDict


class RecentRead(TypedDict):
    tool_name: str
    scope: dict
    turn_id: str


def replace_recent_read(_previous, current):
    """Tool batches are ordered; a later update or explicit None replaces the hint."""
    return current


def bound_recent_read(value, config):
    from agent.runtime.task_scope import binding

    if (not isinstance(value, dict) or set(value) != {"tool_name", "scope", "turn_id"}
            or not isinstance(value["tool_name"], str) or not value["tool_name"]
            or not isinstance(value["turn_id"], str) or not value["turn_id"]):
        return None
    try:
        return value if value["scope"] == binding(config) else None
    except ValueError:
        return None


@dataclass(frozen=True)
class ConversationSnapshot:
    messages: list = field(default_factory=list)
    pending_confirm: dict | None = None
    pending_action: dict | None = None
    approval: dict | None = None
    run: dict | None = None
    receipts: list = field(default_factory=list)
    last_outcome: str | None = None
    recent_read: RecentRead | None = None
    unfinished: bool = False
    initialized: bool = False

    @property
    def has_pending(self) -> bool:
        return bool(self.pending_confirm or self.pending_action)

    @property
    def pending(self) -> dict:
        return {"pending_confirm": self.pending_confirm, "pending_action": self.pending_action}


class ConversationStore(Protocol):
    @property
    def saver(self) -> Any:
        """Existing saver, also used by the independent durable execution ledger."""
        ...

    @property
    def available(self) -> bool:
        """Whether this runtime can read and save persistent conversation state."""
        ...

    def load(self, config) -> ConversationSnapshot: ...

    def clear_pending(self, config, jti: str, *, observations=()) -> None:
        """Clear the matching card and append its optional facts in the same commit."""
        ...

    def guard(self, config) -> AbstractContextManager: ...

    def is_resuming(self, config) -> bool: ...

    def prune(self, config) -> None: ...
