"""Verified actor and scope checks shared by application adapters."""

from agent.authz import SCOPE_READ_OWN, holds
from agent.principal import is_verified_identity


def scoped_reads_enabled(principal, scope: str, user_id=None) -> bool:
    return (is_verified_identity(principal) and holds(principal, scope)
            and (user_id is None or (type(user_id) is int and user_id == principal.uid)))


def own_reads_enabled(principal) -> bool:
    return scoped_reads_enabled(principal, SCOPE_READ_OWN)


def configured_reads_enabled(config, scope: str) -> bool:
    """Read access depends on verified scope and actor consistency, not a writable chat thread."""
    cfg = (config or {}).get("configurable") or {}
    return scoped_reads_enabled(cfg.get("principal"), scope, cfg.get("user_id"))
