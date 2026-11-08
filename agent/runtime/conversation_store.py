"""Conversation data and the storage boundary used by the Agent runtime.

Conversation turns, task identities and framework approval bindings are saved.
Reads without a committed checkpoint may run again; business writes remain owned
by the independent execution ledger.
"""

from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from typing import Any, Protocol, TypedDict


class ConversationAccessDenied(PermissionError):
    """Missing or foreign ownership must not expose or mutate conversation state."""


def owned_config(config):
    """Bind framework checkpoint metadata to the verified actor, never caller metadata."""
    from agent.runtime import task_scope

    cfg = (config or {}).get("configurable") or {}
    try:
        principal = task_scope.caller(config)
        cid, thread = cfg.get("conversation_id"), cfg.get("thread_id")
        background = cfg.get("background_run_id")
        if (not isinstance(thread, str) or not thread or len(thread) > 200
                or "\x00" in thread or (cid is not None and type(cid) not in (int, str))):
            raise ValueError("invalid_conversation_identity")
        if background is not None:
            task_scope.binding(config)
    except ValueError:
        raise ConversationAccessDenied("conversation_not_found") from None
    metadata = {**(config.get("metadata") or {}), "user_id": principal.uid,
                "conversation_id": cid, "background_run_id": background}
    return {**config, "configurable": {**cfg, "user_id": principal.uid}, "metadata": metadata}


def require_owner(metadata, config):
    """The framework's persisted user_id is immutable ownership, not the current role."""
    if metadata is None:
        return
    cfg = config["configurable"]
    if (type(metadata.get("user_id")) is not int or metadata["user_id"] != cfg["user_id"]
            or metadata.get("conversation_id") != cfg.get("conversation_id")
            or metadata.get("background_run_id") != cfg.get("background_run_id")):
        raise ConversationAccessDenied("conversation_not_found")


class DomainActivation(TypedDict):
    domain: str
    turn_id: str | None


class ToolDomains(TypedDict):
    scope: dict | None
    domains: list[DomainActivation]


def bound_tool_domains(value, config):
    """Saved activation is conversation metadata, never an authorization grant."""
    from agent.runtime.task_scope import binding

    if (not isinstance(value, dict) or set(value) != {"scope", "domains"}
            or not isinstance(value["domains"], list)):
        return None
    try:
        if value["scope"] != binding(config):
            return None
    except ValueError:
        return None
    names = set()
    for item in value["domains"]:
        if (not isinstance(item, dict) or set(item) != {"domain", "turn_id"}
                or not isinstance(item["domain"], str) or not item["domain"]
                or (item["turn_id"] is not None and (
                    not isinstance(item["turn_id"], str) or not item["turn_id"]))
                or item["domain"] in names):
            return None
        names.add(item["domain"])
    return value


def replace_tool_domains(_previous, current):
    """Ordered tool batches publish progressively complete activation snapshots."""
    return current


@dataclass(frozen=True)
class ConversationSnapshot:
    messages: list = field(default_factory=list)
    pending_confirm: dict | None = None
    pending_action: dict | None = None
    approval: dict | None = None
    run: dict | None = None
    receipts: list = field(default_factory=list)
    last_outcome: str | None = None
    tool_domains: ToolDomains | None = None
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

    def guard(self, config, *, timeout=60) -> AbstractContextManager:
        """Yield the actor-bound config while serializing a conversation mutation."""
        ...

    def is_resuming(self, config) -> bool: ...

    def prune(self, config) -> None: ...
