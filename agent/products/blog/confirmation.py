"""Adapt signed confirmation execution and lifecycle updates to the server protocol."""

import hashlib
import json
import time


def presentation(result):
    """Keep complete operation details in the card rather than repeating its body."""
    from agent.runtime.results import TurnResult

    execution = result.execution or {}
    if not execution.get("pending_confirm"):
        return result
    reply = "已准备好，请核对卡片；确认后我再执行。"
    return TurnResult(reply, {**execution, "confirm_text": reply})


def confirmed_runtime(runtime, grant):
    from agent.domains.registry import native_domain
    from agent.runtime.service import AgentRuntime

    if isinstance(grant, dict) and grant.get("executor") and native_domain(grant) is None:
        raise ValueError("无法识别确认操作的执行入口，本次未执行。")
    if native_domain(grant) is None:
        return runtime
    if not isinstance(runtime, AgentRuntime):
        raise ValueError("确认执行需要 AgentRuntime，本次未执行。")
    selected = runtime
    if selected.store.saver is None:
        raise RuntimeError("操作确认需要持久化服务，本次未执行。")
    return selected


def cancel(runtime, grant, config):
    """Revoke the signed operation under the same lock used for execution."""
    from agent.domains.registry import native_domain
    from agent.products.blog.pending import PendingOperation
    from agent.runtime.results import cancellation_observations
    if native_domain(grant) is None:
        raise ValueError("这张旧确认卡尚不支持按钮撤销，请勿批准并等待其过期。")
    selected = confirmed_runtime(runtime, grant)
    action = {"task_id": f"pa_{grant['jti']}", "jti": grant["jti"]}
    saver = selected.store.saver
    with selected.conversation_guard(config):
        result = PendingOperation(grant, action).cancel(config, saver)
        if not result.execution:
            return {"cancelled": False, "reply": result.reply}
        facts = cancellation_observations(result.reply)
        selected.store.clear_pending(config, grant["jti"], observations=facts)
        return {"cancelled": True, "event": result.execution["pending_event"],
                "reply": result.reply}


def public_card(execution):
    """Project display fields and the original grant; never expose specs or graph state."""
    popup = execution.get("pending_confirm")
    if not popup:
        return None
    identifier = ((execution.get("pending_action") or {}).get("task_id")
                  or hashlib.sha256(str(popup.get("token", "")).encode()).hexdigest()[:16])
    return {
        "id": identifier, "q": popup.get("q", ""),
        "opts": popup.get("opts") or [], "token": popup.get("token", ""),
        "exp": popup.get("exp") or 0, "server_time": int(time.time()),
        **({"task_id": execution["task_id"]} if execution.get("task_id") else {}),
        "replay_safe": popup.get("replay_safe") is True}


def current_card(runtime, principal, cid, task_id):
    """Read the current checkpoint without queuing behind an in-flight model call."""
    from agent import checkpoint, confirm
    from agent.runtime import approval, task_scope

    config = {"configurable": {"principal": principal, "user_id": principal.uid,
              "conversation_id": cid, "thread_id": checkpoint.conversation_thread(cid)}}
    snapshot = runtime.store.load(config)
    task = snapshot.run
    result = {"task_id": task_id, "confirmation": None}
    if (not task or (task_id is not None and task["id"] != task_id)
            or task["scope"] != task_scope.binding(config)):
        return result
    token = (snapshot.pending_confirm or {}).get("token", "")
    grant = confirm.verify(token, principal.uid, cid)
    if grant is None or not approval.matches(snapshot.approval, grant, config):
        return result
    return {"task_id": task["id"], "confirmation": public_card({
        "pending_confirm": snapshot.pending_confirm, "pending_action": snapshot.pending_action,
        "task_id": task["id"]})}


def frames(execution):
    """Cancellation precedes a replacement card and its host persistence event."""
    event = execution.get("pending_event")
    if event:
        yield "__PENDING__:" + json.dumps(event, ensure_ascii=False)
    card = public_card(execution)
    if card is None:
        return
    yield "__CONFIRM__:" + json.dumps(card, ensure_ascii=False)
    action = execution.get("pending_action")
    if action:
        yield "__PENDING__:" + json.dumps(action, ensure_ascii=False)
