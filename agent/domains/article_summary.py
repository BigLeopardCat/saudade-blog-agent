"""Authorize one version-bound browser draft change and retain its observed result."""

import hashlib
import re

from agent import authz, idem
from agent.runtime import writes
from agent.runtime.checkpoint_conversations import CheckpointConversationStore
from agent.runtime.turns import check_cancelled
from tools.result import ok
from utils.trace import record

PERMISSION_OPERATIONS = frozenset({"set_article_summary"})


def handles(grant):
    return isinstance(grant, dict) and grant.get("workflow") == "editor_summary"


def validate(grant, config):
    principal = writes.validate_binding(grant, config)
    specs = grant.get("specs")
    if (not handles(grant) or grant.get("executor") != "editor_summary"
            or not isinstance(specs, list) or len(specs) != 1
            or not isinstance(specs[0], dict) or specs[0].get("tool") != "set_article_summary"):
        raise ValueError("确认内容与摘要修改不一致")
    args = specs[0].get("args")
    if (not isinstance(args, dict)
            or set(args) != {"editor_id", "revision", "summary_sha256",
                             "previous_summary_sha256", "request_id"}
            or not isinstance(args["editor_id"], str) or not 1 <= len(args["editor_id"]) <= 64
            or type(args["revision"]) is not int or not 0 <= args["revision"] <= 2**53 - 1
            or not isinstance(args["summary_sha256"], str)
            or not re.fullmatch(r"[0-9a-f]{64}", args["summary_sha256"])
            or not isinstance(args["previous_summary_sha256"], str)
            or not re.fullmatch(r"[0-9a-f]{64}", args["previous_summary_sha256"])
            or not isinstance(args["request_id"], str) or not 1 <= len(args["request_id"]) <= 100):
        raise ValueError("摘要确认参数无效")
    if not authz.check(principal, "set_article_summary").allowed:
        raise ValueError("当前身份无权修改文章摘要")
    return "set_article_summary", args


def receipt(operation_id, args, status, text):
    return {"tool": "set_article_summary", "operation_id": operation_id,
            "status": status, "scope": "editor_draft", "published": False,
            "args": {"editor_id": args["editor_id"], "revision": args["revision"],
                     "summary_sha256": args["summary_sha256"]},
            "result": text}


def saved_result(operation, receipt, description):
    return ok(receipt["result"], meta={"operation": operation, "receipt": receipt,
                                      "description": description})


def description_matches(description, args):
    return (isinstance(description, str) and len(description) <= 16000
            and hashlib.sha256(description.encode()).hexdigest() == args["summary_sha256"])


def execute(grant, config, saver, *, ledger=None):
    name, args = validate(grant, config)
    operation = {"tool": name, "args": args}
    store = writes.ledger_for(saver) if ledger is None else ledger
    key = writes.operation_key(grant, name, args)
    previous = store.lookup(key)
    replay_only = (config.get("configurable") or {}).get("confirmation_replay_only")
    if previous is None and not replay_only:
        pending = CheckpointConversationStore(saver).load(config).pending_action or {}
        description = (pending.get("payload") or {}).get("description")
        if pending.get("jti") != grant["jti"] or not description_matches(description, args):
            return {"reply": "待确认摘要已变化或丢失，请重新准备；本次没有更新。",
                    "receipts": [], "blocked": True}
        check_cancelled(config)
        previous = store.claim(key, name, grant["jti"], resuming=True)
        if previous[0] == "new":
            approved = receipt(grant["jti"], args, "approved",
                               "摘要修改已获得确认，应用结果以编辑器回执为准。")
            store.finish(key, saved_result(operation, approved, description))
            previous = store.lookup(key)
            record("native", "client_operation_approved", name=name, operation_id=grant["jti"])
    if previous is None or previous[0] != "done":
        return {"reply": "这次摘要确认已失效或结果未知，请重新读取草稿后准备。", "receipts": [],
                "blocked": True}
    result = idem.replay_result(previous[1])
    meta = getattr(result, "meta", {}) or {}
    row = meta.get("receipt")
    if (meta.get("operation") != operation or not isinstance(row, dict)
            or not description_matches(meta.get("description"), args)):
        return {"reply": "这次摘要确认已撤销，没有更新草稿。", "receipts": [], "blocked": True}
    execution = {"reply": row["result"], "receipts": []}
    if row["status"] == "completed":
        execution["receipts"] = [row]
    elif row["status"] == "unverified":
        execution["operation_outcome"] = {**operation, "status": "unverified",
                                           "result": row["result"]}
    else:
        execution["authorized_operation"] = {
            **operation, "operation_id": grant["jti"], "status": "approved"}
    if row["status"] == "approved" and not replay_only:
        execution["editor_summary_command"] = {
            "operation_id": grant["jti"], "uid": grant["uid"], "conv": grant["conv"],
            "ledger_key": key, "operation": operation, "description": meta["description"]}
    return execution
