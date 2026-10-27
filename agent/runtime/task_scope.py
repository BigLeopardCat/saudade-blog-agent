"""Conversation task identity is independent of permission to read or write tools."""

from agent.principal import is_verified_identity


def binding(config):
    cfg = (config or {}).get("configurable") or {}
    principal, cid = cfg.get("principal"), cfg.get("conversation_id")
    if (not is_verified_identity(principal) or not principal.known_role
            or type(cid) is not int or cid <= 0 or cfg.get("thread_id") != f"conv_{cid}"
            or type(cfg.get("user_id", principal.uid)) is not int
            or cfg.get("user_id", principal.uid) != principal.uid):
        raise ValueError("invalid_task_identity")
    return {"uid": principal.uid, "role": principal.known_role, "conversation_id": cid}
