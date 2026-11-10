"""Trusted conversation facts derived from authorization and renderer contracts."""

import hashlib
import json

from agent.authz import scopes_for
from agent.principal import ADMIN_ROLES, ROLE_ZAKO, is_verified_identity
from agent.stickers import STICKER_NAMES


def conversation_session(principal, conversation_id):
    """Partition routing by verified account, role and conversation without exposing IDs."""
    if (not is_verified_identity(principal) or principal.known_role is None
            or type(conversation_id) is not int or conversation_id <= 0):
        return None
    coordinates = ["saudade-blog-agent:v1", principal.uid, principal.known_role, conversation_id]
    return hashlib.sha256(json.dumps(coordinates, separators=(",", ":")).encode()).hexdigest()


def conversation_context(principal) -> dict:
    from tools.site import public_navigation

    verified = is_verified_identity(principal)
    role = getattr(principal, "known_role", None) if verified else None
    return {
        "rendering": {"stickers": [f":{name}:" for name in STICKER_NAMES],
                      "sticker_markdown": "![name](:name:)",
                      "sticker_catalog": ":全部表情: renders all builtin stickers; no tools needed",
                      "sticker_format": {"markdown_text": "image", "inline_code": "literal",
                                         "code_block": "literal", "markdown_image_url": "image"}},
        "guestbook": {"posting_requires_login": True,
                      "anonymous": "hides public display name, not the authenticated publisher",
                      "publisher_lookup_roles": sorted(ADMIN_ROLES)},
        "article_management": {"requires_admin_console_access": True,
                               "applies_to": ["agent_tools", "web_editor"],
                               "author_ownership_is_not_permission": True},
        "tools": {"scope": "current_caller",
                  "loading": "disabled" if role == ROLE_ZAKO else "on_demand",
                  "administration_requires": sorted(ADMIN_ROLES)},
        "caller": {"verified": verified, "role": role,
                     "can_access_admin_console": verified and role in ADMIN_ROLES,
                     "preferred_address": ("主人" if role in ADMIN_ROLES
                                           else "杂鱼" if role == ROLE_ZAKO else "你")},
        "permissions": sorted(scopes_for(role)),
        "manual_ui": {
            "article_comments": {"location": "after article body"},
            "public_pages": public_navigation() if role == ROLE_ZAKO else {},
            "effects": {"entry": "mascot star button (screen position varies)",
                        "options": ["sakura", "rain", "snow"]},
            "grants_agent_permissions": False,
        },
    }
