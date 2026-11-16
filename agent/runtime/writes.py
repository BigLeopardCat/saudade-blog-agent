"""Identity binding and durable execution shared by confirmed domain operations."""

import hashlib
import json
import time

from agent import checkpoint, idem
from agent.runtime import checkpoint_store
from agent.runtime.access import own_reads_enabled
from agent.runtime.turns import AgentCancelled, check_cancelled
from tools.base import ToolResult, unavailable
from utils.trace import record


def bound_context(config):
    """Verified identity and stable conversation shared by preparation and execution."""
    cfg = (config or {}).get("configurable") or {}
    principal = cfg.get("principal")
    if not own_reads_enabled(principal):
        raise ValueError("需要已验签的当前用户身份")
    if "user_id" in cfg and (type(cfg["user_id"]) is not int or cfg["user_id"] != principal.uid):
        raise ValueError("工具调用身份与已验签身份不一致")
    cid = cfg.get("conversation_id")
    if (type(cid) is not int or cid <= 0
            or cfg.get("thread_id") != checkpoint.conversation_thread(cid)):
        raise ValueError("缺少稳定的会话标识")
    return principal, cid


def validate_binding(grant, config):
    principal, cid = bound_context(config)
    if grant.get("uid") != principal.uid or grant.get("conv") != cid:
        raise ValueError("确认凭据与当前用户或会话不一致")
    if type(grant.get("exp")) is not int or grant["exp"] < time.time():
        raise ValueError("确认凭据已过期")
    operation = grant.get("jti")
    if not isinstance(operation, str) or len(operation) != 32:
        raise ValueError("确认凭据缺少操作标识")
    return principal

def ledger_for(saver):
    from agent import idem_pg

    if not checkpoint_store.postgres(saver):
        raise RuntimeError("确认操作需要持久化的 PostgreSQL 幂等账")
    return idem_pg.PostgresLedger(saver.conn)


def operation_key(grant, name, args):
    identity = [grant["uid"], grant["conv"], grant["jti"], name, args]
    return hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()


def execute_once(grant, name, args, config, ledger, tools):
    key = operation_key(grant, name, args)
    previous = ledger.lookup(key)
    if (config.get("configurable") or {}).get("confirmation_replay_only"):
        if previous is None:
            return unavailable("确认操作已失效，请重新发起请求。")
        state, row = previous
    elif previous is not None:
        state, row = previous
    else:
        check_cancelled(config)
        state, row = ledger.claim(key, name, grant["jti"], resuming=True, pending=True)
    if state == "done":
        record("native", "write_replay_hit", name=name, key=key)
        return idem.replay_result(row)
    if state == "cancelled":
        return unavailable("这次操作已在发出前停止，未执行；如仍需操作，请重新准备并确认。",
                           meta={"reason": "write_cancelled_before_dispatch"})
    operation = tools[name]
    cfg = {**config, "configurable": {**config.get("configurable", {}),
                                     "confirmed_operation_id": key}}
    replay_only = (config.get("configurable") or {}).get("confirmation_replay_only")
    recover = getattr(operation, "recover", None)
    if state == "recoverable" and callable(recover):
        check_cancelled(config)
        out = recover(args, config=cfg, retry=not replay_only)
        if getattr(out, "kind", None) == "ok":
            ledger.finish(key, out)
            record("native", "write_recovered", name=name, key=key)
        elif isinstance(out, ToolResult):
            out = ToolResult(str(out), out.kind, {**out.meta, "operation_id": key}, out.data)
        return out
    if state not in ("new", "claimed") or replay_only:
        return idem.unknown_frame(name)
    try:
        check_cancelled(config)
    except AgentCancelled:
        ledger.cancel_claim(key)
        record("native", "write_not_sent", name=name, key=key)
        raise
    if not ledger.dispatch(key, recoverable=callable(recover)):
        return idem.unknown_frame(name)
    # The marker precedes the call: an exception cannot prove whether a write was sent.
    out = operation.invoke(args, config=cfg)
    # Store an observed result even if the caller stopped. Recoverable tools keep
    # unverified outcomes pending until the business service can supply a receipt.
    final = (not callable(recover) or getattr(out, "kind", None) == "ok"
             or getattr(out, "meta", {}).get("reason") == "receipt_mismatch")
    if final:
        ledger.finish(key, out)
    elif isinstance(out, ToolResult):
        out = ToolResult(str(out), out.kind, {**out.meta, "operation_id": key}, out.data)
    record("native", "write_done" if final else "write_pending", name=name, key=key)
    return out


def execute_confirmed(grant, config, saver, *, validate, receipt, tool_factory, ledger=None):
    try:
        name, args = validate(grant, config)
    except ValueError as exc:
        return {"reply": f"{exc}，本次未执行。", "receipts": [],
                "blocked": [{"reason": "invalid_args"}]}
    try:
        store = ledger_for(saver) if ledger is None else ledger
        result = execute_once(grant, name, args, config, store, tool_factory(name))
    except Exception as exc:
        from agent.runtime.turns import AgentCancelled
        if isinstance(exc, AgentCancelled):
            raise
        record("native", "write_error", name=name, error=type(exc).__name__)
        result = unavailable("无法确认本次操作的执行结果，请先查询实际状态，不要重复执行。",
                             meta={"operation_id": operation_key(grant, name, args)})
    evidence = receipt(grant, name, args, result)
    if evidence is None:
        reply = (str(result) if isinstance(result, ToolResult)
                 and (result.kind != "ok" or (result.meta or {}).get("reason"))
                 else "工具未提供有效回执，无法确认改动。")
        failure = {"reply": reply, "receipts": [],
                   "blocked": [{"tool": name, "reason": "error_frame"}]}
        # A failed observation is not evidence that the business write did not happen.
        failure["operation_outcome"] = {
            "status": "unverified", "tool": name, "args": args, "result": reply}
        identifier = getattr(result, "meta", {}).get("operation_id")
        if identifier:
            failure["operation_outcome"]["operation_id"] = identifier
        return failure
    return {"reply": str(result), "receipts": [evidence], "blocked": []}
