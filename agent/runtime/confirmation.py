"""Adapt signed confirmation execution and lifecycle updates to the server protocol."""

import json
import uuid


def proposal(principal, cid, workflow, spec, *, executor, question, target, reply):
    from agent import adminops_confirm, confirm
    from agent.runtime.results import TurnResult
    from tools.base import unavailable

    specs = [spec]
    token = confirm.sign(principal.uid, cid, workflow, specs, executor=executor)
    if not token:
        return unavailable("确认服务暂不可用，本次未生成确认卡，也未执行改动。")
    expiry, jti = confirm.token_expiry(token), confirm.token_jti(token)
    options = adminops_confirm.confirm_opts(1)
    return TurnResult(reply, {
        "pending_confirm": {"q": question, "opts": options, "token": token,
                            "specs": specs, "workflow": workflow, "exp": expiry},
        "pending_action": {"task_id": f"pa_{jti}", "skill": workflow, "specs": specs,
                           "target": target, "requested_by": "user",
                           "source_event": "confirm_popup", "question": question,
                           "options": options, "token": token, "jti": jti,
                           "expires_at": expiry},
        "confirm_text": reply, "receipts": []})


def confirmed_runtime(runtime, grant):
    if (grant or {}).get("executor") not in {"favorites", "notifications"}:
        return runtime
    from agent.runtime.hybrid import HybridArm
    # Changing the default runtime cannot bypass revocation or replay checks.
    selected = runtime if isinstance(runtime, HybridArm) else HybridArm(runtime)
    if getattr(selected.legacy, "checkpointer", None) is None:
        raise RuntimeError("操作确认需要持久化服务，本次未执行。")
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
