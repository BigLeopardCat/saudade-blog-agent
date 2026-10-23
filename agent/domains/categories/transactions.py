"""Execute one signed category change through a serializable business transaction."""

import time

from pydantic import BaseModel, ConfigDict, Field, StrictStr

from agent import authz
from agent.domains.categories import targets
from agent.domains.categories.preparation import TOOLS, WORKFLOWS, description
from agent.runtime import writes
from agent.runtime.turns import check_cancelled
from tools import base
from tools.result import fact, ok, tgt


class Arguments(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")
    intent: targets.Intent


def handles(grant):
    return (
        isinstance(grant, dict)
        and grant.get("executor") == "categories"
        and grant.get("workflow") in WORKFLOWS.values()
    )


def validate(grant, config):
    principal = writes.validate_binding(grant, config)
    specs = grant.get("specs")
    if not handles(grant) or not isinstance(specs, list) or len(specs) != 1:
        raise ValueError("确认凭据不是单项分类操作")
    if not isinstance(specs[0], dict):
        raise ValueError("分类确认明细无效")
    args = Arguments.model_validate(specs[0].get("args")).model_dump()
    request = targets.intent(args["intent"])
    name = TOOLS[request["action"]]
    if (
        specs[0].get("tool") != name
        or grant["workflow"] != WORKFLOWS[request["action"]]
        or not authz.check(principal, name).allowed
    ):
        raise ValueError("分类确认操作、工具或权限不匹配")
    return name, args


def outcome(value, args, plan):
    state = targets.plan(value["state"], args["intent"], committed=True)
    expected = dict(plan)
    if args["intent"]["action"] == "create":
        expected = {**plan, "after": {**plan["after"], "id": state["after"]["id"]}}
    if (
        state != expected
        or value.get("beforeVersion") != args["version"]
        or type(value.get("changed")) is not bool
        or value["changed"] != (plan["before"] != plan["after"])
    ):
        raise ValueError("分类实际结果与确认计划不一致")
    return state


class Operation:
    def invoke(self, args, config=None):
        plan = targets.prepare(args["intent"], config)
        if isinstance(plan, base.ToolResult):
            return plan
        if plan["version"] != args["version"]:
            return base.unavailable("分类或关联范围已变化，请重新核对并确认，本次未改动。")
        check_cancelled(config)
        result = base._admin_request("POST", targets.PATH, args, config)
        if isinstance(result, base.ToolResult):
            return result
        try:
            state = outcome(result, args, plan)
        except (ValueError, TypeError, KeyError):
            return base.unavailable("分类请求已发出，但实际结果无法确认；请先查询，不要重复执行。")
        row = state["after"] or state["before"]
        text = "已执行：" + description(state) + "。"
        return ok(
            text,
            meta=fact(
                "category_change",
                changed=result["changed"],
                target=tgt("category", row["id"], row["name"]),
                confirmed_args=args,
                plan=plan,
                outcome=result,
            ),
        )


def _receipt(grant, name, args, result):
    meta = getattr(result, "meta", {}) or {}
    if (
        not isinstance(result, base.ToolResult)
        or result.kind != "ok"
        or not isinstance(meta, dict)
        or meta.get("op") != "category_change"
        or meta.get("confirmed_args") != args
        or type(meta.get("changed")) is not bool
    ):
        return None
    try:
        plan = targets.plan(meta["plan"], args["intent"])
        if plan["version"] != args["version"]:
            return None
        outcome(meta["outcome"], args, plan)
        if meta["outcome"]["changed"] != meta["changed"]:
            return None
    except (ValueError, TypeError, KeyError):
        return None
    return {
        "skill": grant["workflow"],
        "tool": name,
        "args": args,
        "result": str(result),
        "ts": time.time(),
        "action": str(result),
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
