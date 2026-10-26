"""Trusted conversation facts derived from authorization and renderer contracts."""

import hashlib
import json

from agent.authz import scopes_for
from agent.principal import ADMIN_ROLES, is_verified_identity
from agent.stickers import STICKER_NAMES


def conversation_session(principal, conversation_id):
    """Partition routing by verified account, role and conversation without exposing IDs."""
    if (not is_verified_identity(principal) or principal.known_role is None
            or type(conversation_id) is not int or conversation_id <= 0):
        return None
    coordinates = ["saudade-blog-agent:v1", principal.uid, principal.known_role, conversation_id]
    return hashlib.sha256(json.dumps(coordinates, separators=(",", ":")).encode()).hexdigest()


def conversation_context(principal) -> dict:
    verified = is_verified_identity(principal)
    role = getattr(principal, "known_role", None) if verified else None
    return {
        "rendering": {"stickers": [f":{name}:" for name in STICKER_NAMES]},
        "tools": {"loading": "on_demand"},
        "caller": {"verified": verified, "role": role,
                     "preferred_address": "主人" if role in ADMIN_ROLES else "你"},
        "permissions": sorted(scopes_for(role)),
    }
