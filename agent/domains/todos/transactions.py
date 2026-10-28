"""Execute signed todo arguments once, then verify the stored target."""

import time

from agent import action_text, authz
from agent.domains.todos import targets
from agent.runtime import writes
from agent.runtime.turns import check_cancelled
from tools import base
from tools.result import fact, ok, tgt

_WORKFLOWS = {
    "dashboard_todo_add": "create_dashboard_todo",
    "dashboard_todo_done": "complete_dashboard_todo",
    "dashboard_todo_reschedule": "reschedule_dashboard_todo",
}


PERMISSION_OPERATIONS = frozenset(_WORKFLOWS.values())


def handles(grant):
    return (
        isinstance(grant, dict)
        and grant.get("executor") == "todos"
        and grant.get("workflow") in _WORKFLOWS
    )


def validate(grant, config):
    principal = writes.validate_binding(grant, config)
    specs = grant.get("specs")
    if not handles(grant) or not isinstance(specs, list) or len(specs) != 1:
        raise ValueError("确认凭据不是单件待办操作")
    spec = specs[0]
    name = _WORKFLOWS[grant["workflow"]]
    args = spec.get("args") if isinstance(spec, dict) else None
    expected = (
        {"text", "date"}
        if name == "create_dashboard_todo"
        else {"text", "item_id", "version"}
        | ({"date"} if name == "reschedule_dashboard_todo" else set())
    )
    if not isinstance(args, dict) or set(args) != expected or spec.get("tool") != name:
        raise ValueError("待办确认参数不完整或包含额外参数")
    targets.text(args["text"])
    if "date" in args:
        targets.due_date(args["date"])
    if name != "create_dashboard_todo":
        targets.row(
            {
                "id": args["item_id"],
                "version": args["version"],
                "text": args["text"],
                "done": False,
                "date": None,
            }
        )
    if not authz.check(principal, name).allowed:
        raise ValueError("当前身份无权修改后台个人待办")
    return name, args


def _same(current, args):
    return current is not None and all(current[key] == args[key] for key in ("version", "text"))


def _verified_state(name, args, before, written, after):
    actual = after.get(written["id"])
    if actual is None or actual["text"] != args["text"]:
        return None
    if name == "create_dashboard_todo":
        return (
            actual
            if (
                actual["id"] not in before
                and actual["done"] is False
                and actual["date"] == args["date"]
            )
            else None
        )
    if actual["id"] != args["item_id"]:
        return None
    previous = before[args["item_id"]]
    if name == "complete_dashboard_todo":
        return actual if actual["done"] is True and actual["date"] == previous["date"] else None
    return actual if actual["date"] == args["date"] and actual["done"] == previous["done"] else None


class Operation:
    def __init__(self, workflow, name):
        self.workflow, self.name = workflow, name

    def invoke(self, args, config=None):
        rows, error = targets.snapshot(config)
        if error is not None:
            return error
        create = self.name == "create_dashboard_todo"
        current = None if create else rows.get(args["item_id"])
        if (create and len(rows) >= targets.MAX_ROWS) or (not create and not _same(current, args)):
            return base.unavailable("已确认的待办已变化或列表已满，请重新读取并确认；本次未改动。")
        payload = {"text": args["text"]}
        if not create:
            payload.update(id=args["item_id"], version=args["version"])
        complete = self.name == "complete_dashboard_todo"
        payload.update({"done": True} if complete else {"date": args["date"]})
        suffix = "item" if create else "done" if complete else "date"
        changed = create or (not current["done"] if complete else current["date"] != args["date"])
        if not changed:
            return self._result(args, current, False)
        check_cancelled(config)
        out = base._admin_request("POST", targets.PATH + "/" + suffix, payload, config)
        if isinstance(out, base.ToolResult):
            return out
        try:
            written = targets.row(out)
        except (ValueError, TypeError, KeyError):
            return base.unavailable("待办请求已发出，但返回目标无法核对；结果未知，请先查询状态。")
        after, error = targets.snapshot(config)
        if error is not None:
            return base.unavailable("待办请求已发出，但未能读回列表；结果未知，请先查询状态。")
        actual = _verified_state(self.name, args, rows, written, after)
        if actual is None:
            return base.unavailable(
                "待办请求已发出，但实际状态与确认目标不符；结果未知，请先查询状态。"
            )
        return self._result(args, actual, True)

    def _result(self, args, actual, changed):
        label = {
            "create_dashboard_todo": "已新增",
            "complete_dashboard_todo": "已标记完成",
            "reschedule_dashboard_todo": "已修改排期",
        }[self.name]
        text = (
            f"{label}待办「{args['text']}」（编号 {actual['id']}）。"
            if changed
            else f"待办「{args['text']}」已处于所请求状态，本次无需改动。"
        )
        if self.name != "complete_dashboard_todo":
            text += f"排期：{actual['date'] or '未排期'}。"
        return ok(
            text,
            meta=fact(
                self.workflow,
                changed=changed,
                target=tgt("todo", actual["id"], args["text"]),
                confirmed_args=args,
                state=actual,
            ),
        )


def _receipt(grant, name, args, result):
    meta = getattr(result, "meta", {}) or {}
    if (
        not isinstance(result, base.ToolResult)
        or result.kind != "ok"
        or not isinstance(meta, dict)
        or meta.get("op") != grant["workflow"]
        or meta.get("confirmed_args") != args
        or type(meta.get("changed")) is not bool
    ):
        return None
    try:
        state = targets.row(meta.get("state"))
    except (ValueError, TypeError, KeyError):
        return None
    if state["text"] != args["text"] or ("item_id" in args and state["id"] != args["item_id"]):
        return None
    if (
        name == "complete_dashboard_todo"
        and state["done"] is not True
        or name != "complete_dashboard_todo"
        and state["date"] != args["date"]
        or name == "create_dashboard_todo"
        and (not meta["changed"] or state["done"] is not False)
    ):
        return None
    receipt = {
        "skill": grant["workflow"],
        "tool": name,
        "args": args,
        "result": str(result),
        "ts": time.time(),
    }
    if not meta["changed"]:
        receipt["change"] = "本来就处于所请求状态"
    receipt["action"] = action_text.receipt_action(name, args, receipt)
    return receipt


def execute(grant, config, saver, *, tools=None, ledger=None):
    return writes.execute_confirmed(
        grant,
        config,
        saver,
        validate=validate,
        receipt=_receipt,
        tool_factory=lambda name: {name: Operation(grant["workflow"], name)}
        if tools is None
        else tools,
        ledger=ledger,
    )
