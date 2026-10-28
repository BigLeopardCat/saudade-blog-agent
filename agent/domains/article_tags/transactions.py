"""Execute an approved tag plan once through the atomic business endpoint."""

import time

from pydantic import BaseModel, ConfigDict, Field, StrictStr

from agent import authz
from agent.domains.article_tags import targets
from agent.domains.article_tags.preparation import change_summary
from agent.runtime import writes
from agent.runtime.turns import check_cancelled
from tools import base
from tools.result import fact, ok, tgt

OPERATION = "set_article_tags"
PERMISSION_OPERATIONS = frozenset({OPERATION})


class Arguments(BaseModel):
    model_config = ConfigDict(extra="forbid")
    article_id: targets.Identifier
    version: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")
    intent: targets.Intent


def handles(grant):
    return (
        isinstance(grant, dict)
        and grant.get("executor") == "article_tags"
        and grant.get("workflow") == "article_tags"
    )


def validate(grant, config):
    principal = writes.validate_binding(grant, config)
    specs = grant.get("specs")
    if not handles(grant) or not isinstance(specs, list) or len(specs) != 1:
        raise ValueError("确认凭据不是单项文章标签操作")
    spec = specs[0]
    if not isinstance(spec, dict) or spec.get("tool") != OPERATION:
        raise ValueError("文章标签工具不匹配")
    args = Arguments.model_validate(spec.get("args")).model_dump()
    targets.intent(args["intent"])
    if not authz.check(principal, OPERATION).allowed:
        raise ValueError("当前身份无权修改文章标签")
    return OPERATION, args


class Operation:
    def invoke(self, args, config=None):
        plan = targets.prepare(args["article_id"], args["intent"], config)
        if isinstance(plan, base.ToolResult):
            return plan
        if plan["version"] != args["version"]:
            return base.unavailable("文章或标签已变化，请重新核对并确认，本次未改动。")
        check_cancelled(config)
        result = base._admin_request(
            "POST",
            targets.PATH + str(args["article_id"]),
            {"version": args["version"], "intent": args["intent"]},
            config,
        )
        if isinstance(result, base.ToolResult):
            return result
        try:
            actual = targets.plan(result["state"], args["article_id"])
            changed = result["changed"]
            if (
                type(changed) is not bool
                or result["beforeVersion"] != args["version"]
                or actual["version"] != args["version"]
                or actual["before"] != plan["before"]
                or actual["title"] != plan["title"]
                or not targets.matches(actual["after"], plan["after"])
                or changed != (plan["before"] != plan["after"])
            ):
                raise ValueError("标签回执与确认不一致")
        except (ValueError, TypeError, KeyError):
            return base.unavailable("标签请求已发出，但结果无法确认，请先查询状态，不要重复执行。")
        text = "已更新文章标签。\n" + change_summary(actual, args["intent"])
        return ok(
            text,
            meta=fact(
                "set_tags",
                changed=changed,
                target=tgt("note", args["article_id"], actual["title"]),
                confirmed_args=args,
                plan=plan,
                state=actual,
            ),
        )


def _receipt(grant, name, args, result):
    meta = getattr(result, "meta", {}) or {}
    if (
        not isinstance(result, base.ToolResult)
        or result.kind != "ok"
        or not isinstance(meta, dict)
        or meta.get("op") != "set_tags"
        or meta.get("confirmed_args") != args
        or type(meta.get("changed")) is not bool
    ):
        return None
    try:
        plan = targets.plan(meta.get("plan"), args["article_id"])
        actual = targets.plan(meta.get("state"), args["article_id"])
        if (
            meta["changed"] != (plan["before"] != plan["after"])
            or plan["version"] != args["version"]
            or actual["version"] != args["version"]
            or actual["before"] != plan["before"]
            or actual["title"] != plan["title"]
            or not targets.matches(actual["after"], plan["after"])
        ):
            return None
    except (ValueError, TypeError):
        return None
    return {
        "skill": "article_tags",
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
