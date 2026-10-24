"""Adapt signed confirmation execution and lifecycle updates to the server protocol."""

import json
import uuid


def proposal(principal, cid, workflow, spec, *, executor, question, target):
    from agent import confirm
    from agent.runtime.results import TurnResult
    from tools.base import unavailable

    specs = [spec]
    token = confirm.sign(principal.uid, cid, workflow, specs, executor=executor)
    if not token:
        return unavailable("确认服务暂不可用，本次未生成确认卡，也未执行改动。")
    expiry, jti = confirm.token_expiry(token), confirm.token_jti(token)
    options = [{"label": "确认执行", "value": "yes", "kind": "primary"},
               {"label": "取消操作", "value": "no", "kind": "default"}]
    reply = question + "\n确认后执行；修改需求直接在聊天里说。"
    return TurnResult(reply, {
        "pending_confirm": {"q": question, "opts": options, "token": token,
                            "specs": specs, "workflow": workflow, "exp": expiry,
                            "replay_safe": True},
        "pending_action": {"task_id": f"pa_{jti}", "skill": workflow, "specs": specs,
                           "target": target, "requested_by": "user",
                           "source_event": "confirm_popup", "question": question,
                           "options": options, "token": token, "jti": jti,
                           "expires_at": expiry},
        "confirm_text": reply, "receipts": []})


def confirmed_runtime(runtime, grant):
    from agent.domains.registry import native_domain

    if isinstance(grant, dict) and grant.get("executor") and native_domain(grant) is None:
        raise ValueError("无法识别确认操作的执行入口，本次未执行。")
    if native_domain(grant) is None:
        return runtime
    from agent.runtime.hybrid import HybridArm
    from agent.runtime.native import NativeRuntime
    # Changing the default runtime cannot bypass revocation or replay checks.
    selected = runtime if isinstance(runtime, (HybridArm, NativeRuntime)) else HybridArm(runtime)
    if selected.store.saver is None:
        raise RuntimeError("操作确认需要持久化服务，本次未执行。")
    return selected


def cancel(runtime, grant, config):
    """Revoke the signed operation under the same lock used for execution."""
    from agent.domains.registry import native_domain
    from agent.runtime.pending import PendingOperation
    if native_domain(grant) is None:
        raise ValueError("这张旧确认卡尚不支持按钮撤销，请勿批准并等待其过期。")
    selected = confirmed_runtime(runtime, grant)
    action = {"task_id": f"pa_{grant['jti']}", "jti": grant["jti"]}
    saver = selected.store.saver
    with selected.conversation_guard(config):
        result = PendingOperation(grant, action).cancel(config, saver)
        if not result.execution:
            return {"cancelled": False, "reply": result.reply}
        selected.store.clear_pending(config, grant["jti"])
        return {"cancelled": True, "event": result.execution["pending_event"],
                "reply": result.reply}


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
        "exp": popup.get("exp") or 0,
        "replay_safe": popup.get("replay_safe") is True}, ensure_ascii=False)
    action = execution.get("pending_action")
    if action:
        yield "__PENDING__:" + json.dumps(action, ensure_ascii=False)
