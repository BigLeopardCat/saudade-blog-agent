"""Identity binding and durable execution shared by confirmed domain operations."""

import hashlib
import json
import time

from agent import checkpoint, idem
from agent.runtime.read_tools import own_reads_enabled
from agent.runtime.turns import check_cancelled
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

    if not type(saver).__module__.startswith("langgraph.checkpoint.postgres"):
        raise RuntimeError("确认操作需要持久化的 PostgreSQL 幂等账")
    idem_pg.setup(saver.conn)
    return idem_pg.PostgresLedger(saver.conn)


def operation_key(grant, name, args):
    identity = [grant["uid"], grant["conv"], grant["jti"], name, args]
    return hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()


def execute_once(grant, name, args, config, ledger, tools):
    key = operation_key(grant, name, args)
    state, row = ledger.claim(key, name, grant["jti"], resuming=True)
    if state == "done":
        record("hybrid", "write_replay_hit", name=name, key=key)
        return idem.replay_result(row)
    if state != "new":
        return idem.unknown_frame(name)
    check_cancelled(config)
    # An exception may happen after sending the write. Preserve the intent;
    # a retry must report uncertainty instead of invoking the tool again.
    out = tools[name].invoke(args, config=config)
    # Once the tool has run, finish the ledger even if the caller has stopped.
    # A lost result must not cause a later request to repeat an external write.
    ledger.finish(key, out)
    record("hybrid", "write_done", name=name, key=key)
    return out


def execute_confirmed(grant, config, saver, *, validate, receipt, tool_factory, ledger=None):
    check_cancelled(config)
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
        record("hybrid", "write_error", name=name, error=type(exc).__name__)
        result = unavailable("无法确认本次操作的执行结果，请先查询实际状态，不要重复执行。")
    evidence = receipt(grant, name, args, result)
    if evidence is None:
        reply = (str(result) if isinstance(result, ToolResult)
                 and (result.kind != "ok" or (result.meta or {}).get("reason"))
                 else "工具未提供有效回执，无法确认改动。")
        return {"reply": reply, "receipts": [],
                "blocked": [{"tool": name, "reason": "error_frame"}]}
    return {"reply": str(result), "receipts": [evidence], "blocked": []}
