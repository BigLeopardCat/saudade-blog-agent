"""Conversation data and the storage boundary used by the native runtime.

Only completed turns are saved. Interrupted reads may run again; this interface
neither journals tool steps nor owns business confirmations or execution ledgers.
"""

from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass(frozen=True)
class ConversationSnapshot:
    messages: list = field(default_factory=list)
    pending_confirm: dict | None = None
    pending_action: dict | None = None
    unfinished: bool = False

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

    def save_turn(
        self, config, messages, reply, *, observations=(), execution=None, pending=None
    ) -> None:
        """Save the completed turn while the caller holds guard(config)."""
        ...

    def clear_pending(self, config, jti: str) -> None:
        """Clear only the matching card; preserve messages, receipts and other state."""
        ...

    def guard(self, config) -> AbstractContextManager: ...

    def is_resuming(self, config) -> bool: ...

    def prune(self, config) -> None: ...
