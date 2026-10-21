"""Execute confirmed favorite changes without the legacy planner or an LLM.

The existing confirmation flow resolves targets and signs concrete arguments.
This executor rechecks identity, scope, binding and expiry. The signed operation
ID, rather than graph task metadata, identifies a write across retries/restarts.
The existing ledger records intent before the tool call and retains its receipt.
"""

import hashlib
import json
import time

from agent import action_text, authz, checkpoint, idem
from agent.hybrid_read import own_reads_enabled
from agent.turns import check_cancelled
from tools.base import ToolResult, get_all_tools, unavailable
from utils.trace import record

_WORKFLOWS = {"favorite_add": "add_favorite", "favorite_remove": "remove_favorite"}


def handles(grant) -> bool:
    return isinstance(grant, dict) and grant.get("workflow") in _WORKFLOWS


def _bound_spec(grant, config):
    cfg = (config or {}).get("configurable") or {}
    principal = cfg.get("principal")
    if not own_reads_enabled(principal):
        raise ValueError("需要已验签的当前用户身份")
    if grant.get("uid") != principal.uid or grant.get("conv") != cfg.get("conversation_id"):
        raise ValueError("确认凭据与当前用户或会话不一致")
    cid = cfg.get("conversation_id")
    if (type(cid) is not int or cid <= 0
            or cfg.get("thread_id") != checkpoint.conversation_thread(cid)):
        raise ValueError("缺少稳定的会话标识")
    if type(grant.get("exp")) is not int or grant["exp"] < time.time():
        raise ValueError("确认凭据已过期")
    operation = grant.get("jti")
    if not isinstance(operation, str) or len(operation) != 32:
        raise ValueError("确认凭据缺少操作标识")
    name = _WORKFLOWS[grant["workflow"]]
    specs = grant.get("specs")
    if not isinstance(specs, list) or len(specs) != 1 or not isinstance(specs[0], dict):
        raise ValueError("本路径只接受一件已确认的收藏操作")
    spec = specs[0]
    args = spec.get("args")
    if spec.get("tool") != name or not isinstance(args, dict) or set(args) != {"article_id"}:
        raise ValueError("确认参数与收藏操作不一致")
    if type(args["article_id"]) is not int or args["article_id"] <= 0:
        raise ValueError("文章标识无效")
    if not authz.check(principal, name).allowed:
        raise ValueError("当前身份没有执行这件操作的权限")
    return name, args


def _ledger(saver):
    from agent import idem_pg

    if not type(saver).__module__.startswith("langgraph.checkpoint.postgres"):
        raise RuntimeError("收藏事务需要持久化的 PostgreSQL 幂等账")
    idem_pg.setup(saver.conn)
    return idem_pg.PostgresLedger(saver.conn)


def operation_key(grant, name, args):
    identity = [grant["uid"], grant["conv"], grant["jti"], name, args]
    return hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()


def _execute(grant, name, args, config, ledger, tools):
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


def _receipt(grant, name, args, result):
    meta = getattr(result, "meta", {}) or {}
    if not isinstance(meta, dict):
        return None
    target = meta.get("target") or {}
    if (not isinstance(result, ToolResult) or result.kind != "ok"
            or meta.get("op") != grant["workflow"]
            or type(meta.get("changed")) is not bool
            or not isinstance(target, dict) or target.get("kind") != "note"
            or target.get("id") != args["article_id"]):
        return None
    receipt = {"skill": grant["workflow"], "tool": name,
               "args": {"article_id": str(args["article_id"])},
               "result": str(result)[:200], "ts": time.time()}
    if not meta["changed"]:
        receipt["change"] = str(meta.get("change") or "状态已达成，未改动")[:120]
    receipt["action"] = action_text.receipt_action(name, receipt["args"], receipt)
    return receipt


def execute(grant, config, saver, *, tools=None, ledger=None):
    check_cancelled(config)
    name = _WORKFLOWS[grant["workflow"]]
    try:
        name, args = _bound_spec(grant, config)
    except ValueError as exc:
        return {"reply": f"{exc}，本次未执行。", "receipts": [],
                "blocked": [{"tool": name, "reason": "invalid_args"}]}
    selected = ({tool.name: tool for tool in get_all_tools() if tool.name == name}
                if tools is None else tools)
    try:
        store = _ledger(saver) if ledger is None else ledger
        result = _execute(grant, name, args, config, store, selected)
    except Exception as exc:
        from agent.turns import AgentCancelled
        if isinstance(exc, AgentCancelled):
            raise
        record("hybrid", "write_error", name=name, error=type(exc).__name__)
        result = unavailable("无法确认本次收藏操作的执行结果，请先查询实际状态，不要重复执行。")
    receipt = _receipt(grant, name, args, result)
    if receipt is None:
        reply = (str(result) if isinstance(result, ToolResult)
                 and (result.kind != "ok" or (result.meta or {}).get("reason"))
                 else "工具未提供有效回执，无法确认改动。")
        return {"reply": reply, "receipts": [],
                "blocked": [{"tool": name, "reason": "error_frame"}]}
    return {"reply": str(result), "receipts": [receipt], "blocked": []}
