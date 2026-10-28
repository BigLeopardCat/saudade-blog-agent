"""Execute one signed article state change and verify its exact target."""

import time

from agent import authz
from agent.domains.article_actions import batch, targets
from agent.runtime import writes
from agent.runtime.turns import check_cancelled
from tools import base
from tools.result import fact, ok, tgt


def handles(grant):
    return (
        isinstance(grant, dict)
        and grant.get("executor") == "article_state"
        and grant.get("workflow") == "set_status"
    )


def validate(grant, config):
    principal = writes.validate_binding(grant, config)
    specs = grant.get("specs")
    if not handles(grant) or not isinstance(specs, list) or len(specs) != 1:
        raise ValueError("确认凭据不是单项文章状态操作")
    spec = specs[0]
    args = spec.get("args") if isinstance(spec, dict) else None
    if isinstance(spec, dict) and spec.get("tool") == batch.NAME:
        if not authz.check(principal, "set_article_status").allowed:
            raise ValueError("当前身份无权修改文章状态")
        return batch.NAME, batch.validate(args)
    if (
        not isinstance(args, dict)
        or spec.get("tool") != "set_article_status"
        or set(args) != {"article_id", "version", "status", "is_top"}
        or type(args["article_id"]) is not int
        or args["article_id"] <= 0
        or not isinstance(args["version"], str)
        or len(args["version"]) != 64
        or any(char not in "0123456789abcdef" for char in args["version"])
        or args["status"] not in (None, "public", "private", "draft")
        or (
            args["is_top"] is not None
            and (type(args["is_top"]) is not int or args["is_top"] not in (0, 1))
        )
        or (args["status"] is None and args["is_top"] is None)
    ):
        raise ValueError("文章确认参数无效")
    if not authz.check(principal, "set_article_status").allowed:
        raise ValueError("当前身份无权修改文章状态")
    return "set_article_status", args


def _result(args, current, changed):
    label = "已修改文章状态" if changed else "文章已处于目标状态，本次无需改动"
    return ok(
        f"{label}（编号 {current['id']}）：{targets.description(args)}。",
        meta=fact(
            "set_status",
            changed=changed,
            target=tgt("note", current["id"], current["title"]),
            confirmed_args=args,
            state=current,
        ),
    )


class Operation:
    def invoke(self, args, config=None):
        before = targets.read(args["article_id"], config)
        if isinstance(before, base.ToolResult):
            return before
        if before["version"] != args["version"]:
            return base.unavailable("文章已变化，请重新核对并确认，本次未改动。")
        if targets.satisfied(before, args):
            return _result(args, before, False)
        check_cancelled(config)
        result = base._admin_request(
            "POST",
            targets.PATH + str(args["article_id"]),
            {"version": args["version"], "status": args["status"], "isTop": args["is_top"]},
            config,
        )
        if isinstance(result, base.ToolResult):
            return result
        after = targets.read(args["article_id"], config)
        if isinstance(after, base.ToolResult):
            return base.unavailable("文章请求已发出，但无法读回实际状态；结果未知，请先查询。")
        try:
            written = targets.row(result["state"])
            if (
                result.get("changed") is not True
                or result.get("beforeVersion") != args["version"]
                or written != after
                or after["id"] != args["article_id"]
                or not targets.satisfied(after, args)
            ):
                raise ValueError("Article outcome mismatch")
        except (ValueError, TypeError, KeyError, AttributeError):
            return base.unavailable("文章请求已发出，但实际状态无法确认；请先查询，不要重复执行。")
        return _result(args, after, True)


def _receipt(grant, name, args, result):
    if name == batch.NAME:
        return batch.receipt(name, args, result)
    meta = getattr(result, "meta", {}) or {}
    if (
        not isinstance(result, base.ToolResult)
        or result.kind != "ok"
        or not isinstance(meta, dict)
        or meta.get("op") != "set_status"
        or meta.get("confirmed_args") != args
        or type(meta.get("changed")) is not bool
    ):
        return None
    try:
        state = targets.row(meta.get("state"))
    except (ValueError, TypeError, KeyError):
        return None
    if state["id"] != args["article_id"] or not targets.satisfied(state, args):
        return None
    receipt = {
        "skill": "set_status",
        "tool": name,
        "args": args,
        "result": str(result),
        "ts": time.time(),
        "action": str(result),
    }
    if not meta["changed"]:
        receipt["change"] = "状态已达成，未改动"
    return receipt


def execute(grant, config, saver, *, tools=None, ledger=None):
    return writes.execute_confirmed(
        grant,
        config,
        saver,
        validate=validate,
        receipt=_receipt,
        tool_factory=lambda name: {name: batch.Operation() if name == batch.NAME else Operation()}
        if tools is None else tools,
        ledger=ledger,
    )
