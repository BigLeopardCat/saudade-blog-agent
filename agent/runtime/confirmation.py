"""Adapt signed confirmation execution and lifecycle updates to the server protocol."""

import json
import uuid


def confirmed_runtime(runtime, grant):
    if (grant or {}).get("executor") != "favorites":
        return runtime
    from agent.runtime.hybrid import HybridArm
    # Changing the default runtime cannot bypass revocation or replay checks.
    selected = runtime if isinstance(runtime, HybridArm) else HybridArm(runtime)
    if getattr(selected.legacy, "checkpointer", None) is None:
        raise RuntimeError("收藏确认需要持久化服务，本次未执行。")
    return selected


def frames(execution):
    """Cancellation precedes a replacement card and its host persistence event."""
    event = execution.get("pending_event")
    if event:
        yield "__PENDING__:" + json.dumps(event, ensure_ascii=False)
    popup = execution.get("pending_confirm")
    if not popup:
        return
    yield "__CONFIRM__:" + json.dumps({
        "id": uuid.uuid4().hex[:8], "q": popup.get("q", ""),
        "opts": popup.get("opts") or [], "token": popup.get("token", ""),
        "exp": popup.get("exp") or 0}, ensure_ascii=False)
    action = execution.get("pending_action")
    if action:
        yield "__PENDING__:" + json.dumps(action, ensure_ascii=False)
