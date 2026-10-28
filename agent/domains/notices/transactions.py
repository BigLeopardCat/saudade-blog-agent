"""Send a signed notice once; unknown outcomes never trigger another send."""

import time

from pydantic import BaseModel, ConfigDict, StrictBool

from agent import authz
from agent.domains.notices import targets
from agent.domains.notices.preparation import TOOL, WORKFLOW, quoted
from agent.runtime import writes
from agent.runtime.turns import check_cancelled
from tools import base
from tools.result import fact, ok, tgt

PERMISSION_OPERATIONS = frozenset({TOOL})


class Arguments(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: targets.Version
    intent: targets.Intent


class Outcome(BaseModel):
    model_config = ConfigDict(extra="forbid")
    notificationId: targets.Identifier
    delivered: StrictBool
    state: targets.Plan


def handles(grant):
    return (
        isinstance(grant, dict)
        and grant.get("executor") == "notices"
        and grant.get("workflow") == WORKFLOW
    )


def validate(grant, config):
    principal = writes.validate_binding(grant, config)
    specs = grant.get("specs")
    if (
        not handles(grant)
        or not isinstance(specs, list)
        or len(specs) != 1
        or not isinstance(specs[0], dict)
        or specs[0].get("tool") != TOOL
        or not authz.check(principal, TOOL).allowed
    ):
        raise ValueError("个人通知确认操作或权限不匹配")
    args = Arguments.model_validate(specs[0].get("args")).model_dump()
    targets.intent(args["intent"])
    return TOOL, args


def outcome(value, args, plan):
    result = Outcome.model_validate(value).model_dump()
    state = targets.plan(result["state"], args["intent"])
    if not result["delivered"] or state != plan or state["version"] != args["version"]:
        raise ValueError("通知实际结果与确认内容不一致")
    return result


class Operation:
    def invoke(self, args, config=None):
        plan = targets.prepare(args["intent"], config)
        if isinstance(plan, base.ToolResult):
            return plan
        if plan["version"] != args["version"]:
            return base.unavailable("收件人或操作权限已变化，请重新确认，本次未发送。")
        check_cancelled(config)
        result = base._admin_request("POST", targets.PATH, args, config)
        if isinstance(result, base.ToolResult):
            return result
        try:
            result = outcome(result, args, plan)
        except (TypeError, ValueError):
            return base.unavailable("通知请求已发出，但结果无法确认；请先查询，不要重复发送。")
        recipient = plan["recipient"]
        text = (
            f"已发送个人通知 #{result['notificationId']} 给账号 #{recipient['id']} "
            f"{quoted(recipient['username'])}。"
        )
        return ok(
            text,
            meta=fact(
                "notice_send",
                changed=True,
                target=tgt("user", recipient["id"], recipient["username"]),
                confirmed_args=args,
                plan=plan,
                outcome=result,
            ),
        )


def _receipt(grant, name, args, result):
    meta = getattr(result, "meta", {})
    if (
        not isinstance(result, base.ToolResult)
        or result.kind != "ok"
        or not isinstance(meta, dict)
        or meta.get("op") != "notice_send"
        or meta.get("changed") is not True
        or meta.get("confirmed_args") != args
    ):
        return None
    try:
        plan = targets.plan(meta["plan"], args["intent"])
        outcome(meta["outcome"], args, plan)
    except (KeyError, TypeError, ValueError):
        return None
    return {
        "skill": WORKFLOW,
        "tool": name,
        "args": args,
        "result": str(result),
        "action": str(result),
        "ts": time.time(),
    }


def execute(grant, config, saver, *, tools=None, ledger=None):
    return writes.execute_confirmed(
        grant,
        config,
        saver,
        validate=validate,
        receipt=_receipt,
        tool_factory=lambda name: {name: Operation()} if tools is None else tools,
        ledger=ledger,
    )
