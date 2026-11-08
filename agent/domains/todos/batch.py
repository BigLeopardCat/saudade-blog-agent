"""Prepare a selectable todo deletion group and execute its selected subset atomically."""

import json
import time
from typing import Annotated

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool
from pydantic import Field, StrictInt

from agent import authz
from agent.domains.todos import targets
from agent.proposals import proposal
from agent.runtime import writes
from agent.runtime.tool_evidence import observed
from agent.runtime.turns import check_cancelled
from tools import base
from tools.result import invalid_args

NAME = "delete_dashboard_todos"
WORKFLOW = "dashboard_todo_delete_batch"


@tool(return_direct=True)
def prepare_dashboard_todo_deletions(
    item_ids: Annotated[list[StrictInt], Field(min_length=1, max_length=50)],
    config: RunnableConfig,
) -> object:
    """Prepare ONE selectable card to delete multiple of the caller's dashboard todos.
    Use this instead of repeated single-item preparations when deleting multiple todos.
    First identify the exact IDs from the user's request or owned todo list. This tool
    reads versions itself. The user may select any subset; the backend deletes that
    subset in one transaction, or changes nothing if a selected item has changed.
    Other business transactions keep their own approval cards. Does not execute.
    """
    if not targets.available(config):
        return base.unavailable("需要已验签的管理员身份和稳定会话。")
    if len(set(item_ids)) != len(item_ids) or any(item <= 0 for item in item_ids):
        return invalid_args("待办编号必须为不同的正整数。")
    check_cancelled(config)
    data = base._admin_read_post(targets.PATH + "/batch/prepare", {"ids": item_ids}, config)
    if isinstance(data, base.ToolResult):
        return data
    try:
        rows = sorted([targets.row(item) for item in data], key=lambda row: row["id"])
        if [row["id"] for row in rows] != sorted(item_ids):
            raise ValueError("incomplete selection")
    except (ValueError, TypeError, KeyError):
        return base.unavailable("批量待办目标或版本无法核对，本次未准备删除。")
    observed("todos", {"items": rows}, ("items",))
    specs = [
        {
            "tool": "delete_dashboard_todo",
            "args": {"item_id": row["id"], "version": row["version"], "text": row["text"]},
        }
        for row in rows
    ]
    principal, cid = writes.bound_context(config)
    return proposal(
        principal,
        cid,
        WORKFLOW,
        specs,
        executor="todos",
        target=f"删除所选的 {len(rows)} 项待办",
        question="选择要删除的待办；未勾选的保留。",
        notices=[{"kind": "warning", "text": "删除后不可恢复。"}],
        choices=[f"{row['text']}（#{row['id']}）" for row in rows],
    )


def validate(grant, config):
    principal = writes.validate_binding(grant, config)
    specs = grant.get("specs")
    if (
        grant.get("executor") != "todos"
        or grant.get("workflow") != WORKFLOW
        or not isinstance(specs, list)
        or not 1 <= len(specs) <= 50
        or not authz.check(principal, NAME).allowed
    ):
        raise ValueError("无效的批量待办确认")
    items = []
    for spec in specs:
        args = spec.get("args") if isinstance(spec, dict) else None
        if (
            not isinstance(args, dict)
            or set(args) != {"item_id", "version", "text"}
            or spec.get("tool") != "delete_dashboard_todo"
        ):
            raise ValueError("批量待办确认参数不完整")
        row = targets.row(
            {
                "id": args["item_id"],
                "version": args["version"],
                "text": args["text"],
                "done": False,
                "date": None,
            }
        )
        items.append({"id": row["id"], "version": row["version"], "text": row["text"]})
    if len({item["id"] for item in items}) != len(items):
        raise ValueError("批量待办编号不能重复")
    return NAME, {"items": sorted(items, key=lambda item: item["id"])}


def invoke(args, config):
    check_cancelled(config)
    result = base._admin_request("DELETE", targets.PATH + "/batch", args, config)
    if isinstance(result, base.ToolResult):
        return result
    expected = [
        {"id": item["id"], "version": item["version"], "deleted": True} for item in args["items"]
    ]
    if result != expected or not all(
        type(item.get("deleted")) is bool and type(item.get("id")) is int for item in result
    ):
        return base.unavailable("批量删除回执无法完整核对，结果未知，请先查询待办。")
    return base.ok(
        json.dumps({"status": "completed", "deleted": args["items"]}, ensure_ascii=False),
        meta={"confirmed_args": args, "deleted": expected},
    )


def receipt(grant, args, result):
    meta = getattr(result, "meta", {}) or {}
    expected = [
        {"id": item["id"], "version": item["version"], "deleted": True} for item in args["items"]
    ]
    if (
        not isinstance(result, base.ToolResult)
        or result.kind != "ok"
        or meta.get("confirmed_args") != args
        or meta.get("deleted") != expected
    ):
        return None
    return {
        "skill": WORKFLOW,
        "tool": NAME,
        "args": args,
        "result": str(result),
        "action": f"已删除所选 {len(expected)} 项待办。",
        "ts": time.time(),
    }
