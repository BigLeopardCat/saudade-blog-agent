"""Verified task identity for foreground conversations and isolated background runs."""

import hashlib

from agent.principal import is_verified_identity


def background_thread(owner, run_id):
    return "background_" + hashlib.sha256(f"{owner}:{run_id}".encode()).hexdigest()


def caller(config):
    """Resolve the verified actor independently of conversation ownership or role scope."""
    cfg = (config or {}).get("configurable") or {}
    principal = cfg.get("principal")
    if (not is_verified_identity(principal)
            or type(cfg.get("user_id", principal.uid)) is not int
            or cfg.get("user_id", principal.uid) != principal.uid):
        raise ValueError("invalid_task_identity")
    return principal


def binding(config):
    cfg = (config or {}).get("configurable") or {}
    principal, cid = caller(config), cfg.get("conversation_id")
    if not principal.known_role:
        raise ValueError("invalid_task_identity")
    background = cfg.get("background_run_id")
    if background is not None:
        if (type(background) is not str or not background or len(background) > 200
                or "\x00" in background or cid is not None
                or cfg.get("thread_id") != background_thread(principal.uid, background)):
            raise ValueError("invalid_background_identity")
        return {"uid": principal.uid, "role": principal.known_role, "background_run_id": background}
    if type(cid) is not int or cid <= 0 or cfg.get("thread_id") != f"conv_{cid}":
        raise ValueError("invalid_task_identity")
    return {"uid": principal.uid, "role": principal.known_role, "conversation_id": cid}
