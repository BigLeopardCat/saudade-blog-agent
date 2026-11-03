"""Validate stable todo identities and read bounded actor-owned snapshots."""

from datetime import date, datetime

from agent.domains.admin_writes import available as available
from agent.runtime.tool_evidence import observed
from tools import base

PATH = "/api/protected/todos"


def due_date(value):
    if value is None:
        return None
    if (
        not isinstance(value, str)
        or len(value) != 10
        or date.fromisoformat(value).isoformat() != value
    ):
        raise ValueError("排期必须是完整的 YYYY-MM-DD；清空请传 null")
    return value


def text(value):
    if not isinstance(value, str) or value != value.strip() or not 1 <= len(value) <= 200:
        raise ValueError("待办正文须为 1 至 200 字，保留原文且去除首尾空白")
    return value


def row(value):
    if (
        not isinstance(value, dict)
        or type(value.get("id")) is not int
        or value["id"] <= 0
        or type(value.get("done")) is not bool
        or "date" not in value
        or not isinstance(value.get("version"), str)
        or not 1 <= len(value["version"]) <= 40
    ):
        raise ValueError("未提供可核对的待办编号、版本或状态")
    datetime.fromisoformat(value["version"])
    return {
        "id": value["id"],
        "version": value["version"],
        "text": text(value.get("text")),
        "done": value["done"],
        "date": due_date(value["date"]),
    }


def snapshot(config, item_id):
    data = base._admin_get(f"{PATH}/item/{item_id}", config)
    if isinstance(data, base.ToolResult):
        return None, data
    try:
        rows = {item_id: row(data)}
        if rows and rows[item_id]["id"] != item_id:
            raise ValueError("待办编号与请求不符")
    except (ValueError, TypeError, KeyError):
        return None, base.unavailable("未能核对本人待办及稳定编号，本次未改动。")
    observed("todos", {"items": list(rows.values())}, ("items",))
    return rows, None
