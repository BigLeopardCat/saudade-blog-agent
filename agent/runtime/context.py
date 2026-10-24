"""Trusted conversation facts derived from authorization and renderer contracts."""

from agent.authz import scopes_for
from agent.principal import is_verified_identity
from agent.stickers import STICKER_NAMES


def conversation_context(principal) -> dict:
    verified = is_verified_identity(principal)
    role = getattr(principal, "known_role", None) if verified else None
    return {
        "identity": {"verified": verified, "role": role},
        "permissions": sorted(scopes_for(role)),
        "tools": {"loading": "on_demand"},
        "rendering": {"stickers": [f":{name}:" for name in STICKER_NAMES]},
    }
