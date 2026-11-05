"""One bounded tag intent, one approval and one atomic backend transaction."""

import json
import time
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StrictStr

from agent.domains.article_tags import targets
from agent.domains.article_tags.presentation import change_summary
from agent.proposals import proposal
from agent.runtime.results import TurnResult
from agent.runtime.tool_evidence import observed
from agent.runtime.turns import check_cancelled
from agent.runtime.writes import bound_context
from tools import base
from tools.result import invalid_args

NAME = "set_article_tags_batch"


class Item(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: targets.Identifier
    version: Annotated[StrictStr, Field(pattern=r"^[0-9a-f]{64}$")]


class Arguments(BaseModel):
    model_config = ConfigDict(extra="forbid")
    items: Annotated[list[Item], Field(min_length=1, max_length=20)]
    intent: targets.Intent


def validate(value):
    args = Arguments.model_validate(value).model_dump()
    ids = [item["id"] for item in args["items"]]
    if ids != sorted(set(ids)):
        raise ValueError("批量文章编号必须唯一且按序排列")
    targets.intent(args["intent"])
    return args


def combine(specs):
    if not isinstance(specs, list) or not specs:
        raise ValueError("标签批量清单不能为空")
    parts = []
    for spec in specs:
        if not isinstance(spec, dict) or spec.get("tool") != NAME:
            raise ValueError("标签批量事务不能混入其他操作")
        parts.append(validate(spec.get("args")))
    first = parts[0]
    if any(part["intent"] != first["intent"] for part in parts):
        raise ValueError("标签批量意图不一致")
    return validate(
        {"items": [item for part in parts for item in part["items"]], "intent": first["intent"]}
    )


def plans(ids, intent, config):
    if len(set(ids)) != len(ids):
        return invalid_args("批量文章编号不能重复。")
    rows = base._admin_read_post(
        targets.PATH + "batch/prepare", {"article_ids": ids, "intent": intent}, config
    )
    if isinstance(rows, base.ToolResult):
        return rows
    try:
        if not isinstance(rows, list) or len(rows) != len(ids):
            raise ValueError("incomplete selection")
        result = [targets.plan(row, row["id"]) for row in rows]
        if sorted(row["id"] for row in result) != sorted(ids):
            raise ValueError("target mismatch")
        result = sorted(result, key=lambda row: row["id"])
        observed(
            "article_tags_batch",
            {
                "items": [
                    {key: row[key] for key in ("id", "title", "version", "before")}
                    for row in result
                ]
            },
            ("items",),
        )
        return result
    except (ValueError, TypeError, KeyError):
        return base.unavailable("批量目标或版本无法核对，本次未准备或执行改动。")


def prepare(ids, intent, config, *, schedule=None):
    rows = plans(ids, intent, config)
    if isinstance(rows, base.ToolResult):
        return rows
    changed = rows if schedule is not None else [
        row for row in rows if row["before"] != row["after"]]
    if not changed:
        text = f"所选{len(rows)}篇文章的标签均已达成，无需改动。"
        return TurnResult(text, {"noop_note": text, "noop_text": text, "receipts": []})
    args = validate(
        {
            "items": [{"id": row["id"], "version": row["version"]} for row in changed],
            "intent": intent,
        }
    )
    details = [f"### #{row['id']}\n" + change_summary(row, intent) for row in changed]
    text = f"批量修改{len(changed)}篇文章的标签：\n\n" + "\n\n".join(details)
    if len(changed) < len(rows):
        text += f"\n\n另有{len(rows) - len(changed)}篇已达成，不纳入改动。"
    principal, cid = bound_context(config)
    check_cancelled(config)
    specs = [
        {"tool": NAME, "args": {"items": [item], "intent": args["intent"]}}
        for item in args["items"]
    ]
    return proposal(
        principal,
        cid,
        "article_tags",
        specs,
        executor="article_tags",
        question="选择要修改标签的文章；未勾选的保留原标签。",
        target=text,
        schedule=schedule,
        choices=[f"#{row['id']}\n" + change_summary(row, intent) for row in changed],
    )


def outcomes(rows, args, before):
    if not isinstance(rows, list) or len(rows) != len(args["items"]):
        raise ValueError("incomplete outcomes")
    expected = {row["id"]: row for row in before}
    results = []
    for row in rows:
        state = targets.plan(row["state"], row["state"]["id"])
        plan = expected.pop(state["id"])
        if (
            type(row.get("changed")) is not bool
            or row.get("beforeVersion") != plan["version"]
            or state["version"] != plan["version"]
            or state["before"] != plan["before"]
            or state["title"] != plan["title"]
            or row["changed"] != (plan["before"] != plan["after"])
            or not targets.matches(state["after"], plan["after"])
        ):
            raise ValueError("outcome mismatch")
        results.append({"id": state["id"], "title": state["title"], "changed": row["changed"]})
    return sorted(results, key=lambda row: row["id"])


class Operation:
    def invoke(self, args, config=None):
        before = plans([item["id"] for item in args["items"]], args["intent"], config)
        if isinstance(before, base.ToolResult):
            return before
        if [row["version"] for row in before] != [item["version"] for item in args["items"]]:
            return base.unavailable("文章或标签已变化，整批未执行，请重新核对并确认。")
        check_cancelled(config)
        rows = base._admin_request("POST", targets.PATH + "batch", args, config)
        if isinstance(rows, base.ToolResult):
            return rows
        try:
            items = outcomes(rows, args, before)
        except (ValueError, TypeError, KeyError):
            return base.unavailable("批量请求已发出，但回执无法完整核对；结果未知，请先查询。")
        text = json.dumps(
            {"status": "completed", "intent": args["intent"], "items": items}, ensure_ascii=False
        )
        return base.ok(text, meta={"confirmed_args": args, "plans": before, "outcomes": rows})


def receipt(name, args, result):
    meta = getattr(result, "meta", {}) or {}
    if (
        not isinstance(result, base.ToolResult)
        or result.kind != "ok"
        or meta.get("confirmed_args") != args
    ):
        return None
    try:
        items = outcomes(meta["outcomes"], args, meta["plans"])
    except (ValueError, TypeError, KeyError):
        return None
    return {
        "skill": "article_tags",
        "tool": name,
        "args": args,
        "items": items,
        "result": str(result),
        "action": f"已核对{len(items)}篇文章的标签结果。",
        "ts": time.time(),
    }
