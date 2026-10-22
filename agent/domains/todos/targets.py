"""Validate stable todo identities and read bounded actor-owned snapshots."""

from datetime import date, datetime

from agent.domains.admin_writes import available as available
from tools import base

PATH = "/api/protected/todos"
MAX_ROWS = 200


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


def snapshot(config):
    data = base._admin_get(PATH, config)
    if isinstance(data, base.ToolResult):
        return None, data
    try:
        if not isinstance(data, list) or len(data) > MAX_ROWS:
            raise ValueError("待办列表范围无法核对")
        rows = {item["id"]: item for item in map(row, data)}
        if len(rows) != len(data):
            raise ValueError("待办编号重复")
    except (ValueError, TypeError, KeyError):
        return None, base.unavailable("未能核对完整的本人待办及稳定编号，本次未改动。")
    return rows, None
