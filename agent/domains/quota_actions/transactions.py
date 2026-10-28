"""Execute each signed quota decision once and verify its concrete target."""

import time

from agent import action_text, authz
from agent.domains.quota_actions import targets
from agent.runtime import writes
from agent.runtime.turns import check_cancelled
from tools import base
from tools.result import fact, ok, tgt

WORKFLOWS = {"quota_approve": "approve_quota_request", "quota_reject": "reject_quota_request",
             "quota_reset": "reset_user_quota"}


PERMISSION_OPERATIONS = frozenset(WORKFLOWS.values())


def handles(grant):
    return (isinstance(grant, dict) and grant.get("executor") == "quota_actions"
            and grant.get("workflow") in WORKFLOWS)


def validate(grant, config):
    principal = writes.validate_binding(grant, config)
    specs = grant.get("specs")
    if not handles(grant) or not isinstance(specs, list) or len(specs) != 1:
        raise ValueError("确认凭据不是单项额度操作")
    name = WORKFLOWS[grant["workflow"]]
    spec = specs[0]
    args = spec.get("args") if isinstance(spec, dict) else None
    keys = {"user_id", "name", "version"}
    if name != "reset_user_quota":
        keys.add("request_id")
    if name == "reject_quota_request":
        keys.add("reason")
    if (not isinstance(args, dict) or set(args) != keys or spec.get("tool") != name
            or not targets.positive(args.get("user_id"))
            or not isinstance(args.get("name"), str) or not args["name"].strip()
            or len(args["name"]) > 100):
        raise ValueError("额度确认参数或账号身份不完整")
    targets.version(args["version"])
    if "request_id" in args and not targets.positive(args["request_id"]):
        raise ValueError("额度申请编号无效")
    if "reason" in args:
        targets.reason(args["reason"])
    if not authz.check(principal, name).allowed:
        raise ValueError("当前身份无权操作额度")
    return name, args


def _read(args, config):
    return targets.read(config, request_id=args.get("request_id"), user_id=args["user_id"])


def _identity(state, args):
    return state["userId"] == args["user_id"] and state["name"] == args["name"]


def _review_state(state, name, args, operator):
    row = state["request"]
    return (row["status"] == (1 if name == "approve_quota_request" else 2)
            and row["note"] == args.get("reason") and row.get("handledBy") == operator
            and row["handledAt"] is not None)


def _verified_outcome(name, args, config, before, out):
    reset = name == "reset_user_quota"
    operator, _ = writes.bound_context(config)
    if not isinstance(out, dict) or out.get("changed") is not True:
        raise ValueError("Missing committed change")
    written = targets.state(out.get("state"), args.get("request_id"))
    if not _identity(written, args) or written["role"] != before["role"]:
        raise ValueError("Wrong quota account")
    if reset:
        if written["used"] != 0 or written["unlimited"]:
            raise ValueError("Quota reset not confirmed")
    elif not _review_state(written, name, args, operator.uid):
        raise ValueError("Wrong request decision")
    actual, error = _read(args, config)
    if error is not None or not _identity(actual, args) or actual["role"] != before["role"]:
        raise ValueError("Unable to verify actual account")
    if not reset and (actual["version"] != written["version"] or
                      not _review_state(actual, name, args, operator.uid)):
        raise ValueError("Unable to verify actual request")
    return actual


class Operation:
    def __init__(self, workflow, name):
        self.workflow, self.name = workflow, name

    def invoke(self, args, config=None):
        before, error = _read(args, config)
        if error is not None:
            return error
        reset = self.name == "reset_user_quota"
        if (not _identity(before, args) or before["version"] != args["version"]
                or (not reset and before["request"]["status"] != 0)):
            return base.unavailable("额度目标已变化，请重新读取并确认；本次未改动。")
        if reset and (before["unlimited"] or before["used"] == 0):
            return base.unavailable("该账号没有需要重置的额度，本次未改动。")
        payload = {"version": args["version"]}
        path = f"accounts/{args['user_id']}/reset"
        if not reset:
            path = f"requests/{args['request_id']}/review"
            payload.update(approved=self.name == "approve_quota_request", reason=args.get("reason"))
        check_cancelled(config)
        out = base._admin_request("POST", targets.PATH + "/" + path, payload, config)
        if isinstance(out, base.ToolResult):
            return base.unavailable("额度请求已发出，但未取得有效确认；"
                                    "结果未知，请先查询实际状态。")
        try:
            actual = _verified_outcome(self.name, args, config, before, out)
        except (ValueError, TypeError, KeyError):
            return base.unavailable("额度请求已发出，但读回目标或状态不符；"
                                    "结果未知，请先查询实际状态。")
        text = "已" + action_text.receipt_action(self.name, args, {})
        if not reset:
            text += f"（申请编号{args['request_id']}）"
        if self.name == "reject_quota_request":
            text += "；此次驳回未重置额度。"
        elif actual["unlimited"]:
            text += "；当前账号不限额。"
        else:
            text += f"；当前读回已用{actual['used']}轮，上限{actual['limit']}轮。"
        return ok(text, meta=fact(self.workflow, changed=True,
                  target=tgt("user" if reset else "quota_request",
                             args["user_id"] if reset else args["request_id"], args["name"]),
                  confirmed_args=args, account_name=args["name"], state=actual))


def _receipt(grant, name, args, result):
    meta = getattr(result, "meta", {}) or {}
    if (not isinstance(result, base.ToolResult) or result.kind != "ok"
            or meta.get("op") != grant["workflow"] or meta.get("changed") is not True
            or meta.get("confirmed_args") != args):
        return None
    try:
        state = targets.state(meta.get("state"), args.get("request_id"))
        if not _identity(state, args):
            return None
        if name != "reset_user_quota":
            row = state["request"]
            if (row["status"] != (1 if name == "approve_quota_request" else 2)
                    or row["note"] != args.get("reason")):
                return None
    except (ValueError, TypeError, KeyError):
        return None
    receipt = {"skill": grant["workflow"], "tool": name, "args": args,
               "result": str(result), "ts": time.time()}
    receipt["action"] = action_text.receipt_action(name, args, receipt)
    return receipt


def execute(grant, config, saver, *, tools=None, ledger=None):
    return writes.execute_confirmed(grant, config, saver, validate=validate, receipt=_receipt,
           tool_factory=lambda name: ({name: Operation(grant["workflow"], name)}
                                      if tools is None else tools),
           ledger=ledger)
