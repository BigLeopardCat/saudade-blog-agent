"""Consume the backend's version-bound deletion receipt without replaying a write."""

import time

from agent.runtime.turns import check_cancelled
from tools import base
from tools.result import fact, ok, tgt

from .targets import PATH


def invoke(args, config):
    check_cancelled(config)
    outcome = base._admin_request("DELETE", f"{PATH}/item/{args['item_id']}",
                                  {"version": args["version"], "text": args["text"]}, config)
    if isinstance(outcome, base.ToolResult):
        return outcome
    if (not isinstance(outcome, dict) or set(outcome) != {"id", "version", "deleted"}
            or type(outcome["id"]) is not int or outcome["id"] != args["item_id"]
            or outcome["version"] != args["version"] or outcome["deleted"] is not True):
        return base.unavailable("删除请求已发出，但回执与确认目标不符；结果未知，请先查询状态。")
    return ok(f"已删除待办「{args['text']}」（编号 {args['item_id']}）。", meta=fact(
        "dashboard_todo_delete", changed=True, target=tgt("todo", args["item_id"], args["text"]),
        confirmed_args=args, deleted=outcome))


def receipt(grant, args, result):
    meta = getattr(result, "meta", {}) or {}
    expected = {"id": args["item_id"], "version": args["version"], "deleted": True}
    if (not isinstance(result, base.ToolResult) or result.kind != "ok"
            or meta.get("op") != "dashboard_todo_delete" or meta.get("changed") is not True
            or meta.get("confirmed_args") != args or meta.get("deleted") != expected):
        return None
    return {"skill": grant["workflow"], "tool": "delete_dashboard_todo", "args": args,
            "result": str(result), "action": str(result), "ts": time.time()}
