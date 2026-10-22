"""Trusted conversation facts derived from authorization and renderer contracts."""

from agent.authz import scopes_for
from agent.principal import SOURCE_ASSERTION
from agent.stickers import STICKER_NAMES


def conversation_context(principal) -> dict:
    verified = (getattr(principal, "source", None) == SOURCE_ASSERTION
                and type(getattr(principal, "uid", None)) is int and principal.uid > 0)
    role = getattr(principal, "known_role", None) if verified else None
    return {
        "identity": {"verified": verified, "role": role},
        "permissions": sorted(scopes_for(role)),
        "tools": {"loading": "on_demand", "remaining_operations": "delegate_to_site_agent"},
        "rendering": {"stickers": [f":{name}:" for name in STICKER_NAMES]},
    }
