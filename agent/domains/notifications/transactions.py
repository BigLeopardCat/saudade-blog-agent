"""Mark only signed notification IDs and verify their individual read states."""

import time

from agent import action_text, authz
from agent.domains.notifications.preparation import snapshot
from agent.runtime import writes
from agent.runtime.turns import check_cancelled
from tools import base
from tools.result import fact, ok, tgt


def handles(grant):
    return (isinstance(grant, dict) and grant.get("executor") == "notifications"
            and grant.get("workflow") == "notice_read")


def validate(grant, config):
    principal = writes.validate_binding(grant, config)
    specs = grant.get("specs")
    if not handles(grant) or not isinstance(specs, list) or len(specs) != 1:
        raise ValueError("确认凭据不是单件通知已读操作")
    spec = specs[0]
    args = spec.get("args") if isinstance(spec, dict) else None
    if (not isinstance(spec, dict) or spec.get("tool") != "read_notifications"
            or not isinstance(args, dict) or set(args) != {"ids", "all"}
            or args["all"] is not False):
        raise ValueError("确认参数必须是固定的通知编号范围")
    ids = args["ids"]
    if (not isinstance(ids, list) or not 1 <= len(ids) <= 100
            or any(type(key) is not int or key <= 0 for key in ids)
            or ids != sorted(set(ids))):
        raise ValueError("通知编号无效或不规范")
    if not authz.check(principal, "read_notifications").allowed:
        raise ValueError("当前身份无权标记通知已读")
    return "read_notifications", args


class NotificationRead:
    def invoke(self, args, config=None):
        value, error = snapshot(config)
        if error is not None:
            return error
        _, rows = value
        ids = args["ids"]
        if any(key not in rows for key in ids):
            return base.unavailable("部分已确认通知已不在可核对的列表中，本次未改动。")
        unread = [key for key in ids if not rows[key]["isRead"]]
        if not unread:
            return ok("所选通知已经全部已读，本次无需改动。", meta=fact(
                "notice_read", changed=False, ids=ids, change="本来就读过"))
        check_cancelled(config)
        out = base._own_post("/api/protected/notifications/read",
                             {"ids": unread, "all": False}, config)
        if isinstance(out, base.ToolResult):
            return out
        after, error = snapshot(config)
        if error is not None or any(
                key not in after[1] or after[1][key]["isRead"] is not True for key in ids):
            return base.unavailable("标记请求已发出，但未能核对全部目标已读；"
                                    "结果未知，请先查询状态。")
        return ok(f"已核对所选 {len(ids)} 条通知均已读，本次标记 {len(unread)} 条。",
                  meta=fact("notice_read", changed=True, ids=ids,
                            target=tgt("notice", ids[0] if len(ids) == 1 else None),
                            change=f"标记已读 {len(unread)} 条"))


def _receipt(grant, name, args, result):
    meta = getattr(result, "meta", {}) or {}
    if (not isinstance(result, base.ToolResult) or result.kind != "ok"
            or meta.get("op") != "notice_read" or meta.get("ids") != args["ids"]
            or type(meta.get("changed")) is not bool):
        return None
    receipt = {"skill": "notice_read", "tool": name, "args": args,
               "result": str(result), "ts": time.time()}
    if not meta["changed"]:
        receipt["change"] = meta["change"]
    receipt["action"] = action_text.receipt_action(name, args, receipt)
    return receipt


def execute(grant, config, saver, *, tools=None, ledger=None):
    return writes.execute_confirmed(
        grant, config, saver, validate=validate, receipt=_receipt,
        tool_factory=lambda name: {name: NotificationRead()} if tools is None else tools,
        ledger=ledger)
