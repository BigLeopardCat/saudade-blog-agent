"""Bounded administrator quota requests and personal dashboard todos."""

import json
from datetime import date
from typing import Annotated, Literal
from urllib.parse import urlencode

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool
from pydantic import Field, StrictBool, StrictInt

from agent.authz import SCOPE_ADMIN_CONSOLE
from agent.reports import sanitize_untrusted
from agent.runtime.read_tools import scoped_reads_enabled
from tools import base

Page = Annotated[StrictInt, Field(ge=1, le=10000)]
Limit = Annotated[StrictInt, Field(ge=1, le=10)]
Identifier = Annotated[StrictInt, Field(gt=0)]
Status = Literal["pending", "approved", "rejected", "all"]


def _quota_entry(row, detail):
    if (not isinstance(row, dict) or type(row.get("id")) is not int or row["id"] <= 0
            or type(row.get("userId")) is not int or row["userId"] <= 0
            or type(row.get("status")) is not int or row["status"] not in (0, 1, 2)
            or type(row.get("accountExists")) is not bool):
        raise ValueError("Invalid request identity or status")
    exists = row["accountExists"]
    account = None
    if exists:
        if (any(not isinstance(row.get(key), str) for key in ("username", "nickname"))
                or any(type(row.get(key)) is not int or row[key] < 0 for key in ("used", "limit"))):
            raise ValueError("Invalid account metadata")
        account = {key: sanitize_untrusted(row[key], 80) for key in ("username", "nickname")}
        account.update({"used": row["used"], "limit": row["limit"]})
    entry = {"id": row["id"], "userId": row["userId"], "account": account,
             "status": ("pending", "approved", "rejected")[row["status"]],
             "createdAt": row.get("createdAt"), "handledAt": row.get("handledAt")}
    for key in ("reason", "note"):
        value = row.get(key)
        if value is not None and not isinstance(value, str):
            raise ValueError("Invalid request text")
        cap = (500 if key == "reason" else 255) if detail else 160
        entry[key if detail else key + "Preview"] = (
            sanitize_untrusted(value, cap) if value else value)
        entry[key + "Truncated"] = bool(value and len(value) > cap)
    return entry


def _todo_entry(row, _detail):
    if (not isinstance(row, dict) or not isinstance(row.get("text"), str)
            or not row["text"].strip() or len(row["text"]) > 200
            or type(row.get("done")) is not bool or "date" not in row):
        raise ValueError("Invalid todo")
    due = row["date"]
    if due is not None and (not isinstance(due, str) or date.fromisoformat(due).isoformat() != due):
        raise ValueError("Invalid due date")
    return {"text": sanitize_untrusted(row["text"], 0), "done": row["done"], "date": due}


def _read(resource, config, page, limit, query, entry, detail=False):
    cfg = (config or {}).get("configurable") or {}
    if not scoped_reads_enabled(cfg.get("principal"), SCOPE_ADMIN_CONSOLE, cfg.get("user_id")):
        return base.unavailable("需要已验签且一致的管理员身份。")
    params = {"page": page, "page_size": limit, **query}
    params = {key: str(value).lower() if type(value) is bool else value
              for key, value in params.items() if value is not None}
    data = base._admin_get("/api/protected/" + resource + "?" + urlencode(params),
                           config, paginated=True)
    if isinstance(data, base.ToolResult):
        return data
    if not isinstance(data, dict):
        return base.unavailable("列表返回格式无法核对。")
    rows, total = data.get("items"), data.get("total")
    offset = (page - 1) * limit
    if (not isinstance(rows, list) or len(rows) > limit or type(total) is not int or total < 0
            or len(rows) != min(limit, max(0, total - offset))):
        return base.unavailable("列表缺少可靠总数或分页范围不一致。")
    try:
        entries = [entry(row, detail) for row in rows]
    except (ValueError, TypeError):
        return base.unavailable("记录字段无法核对。")
    expected = {key: value for key, value in query.items()
                if value is not None and not (key == "status" and value == "all")}
    if any(any(row.get(key) != value for key, value in expected.items()) for row in entries):
        return base.unavailable("返回记录与请求范围不一致。")
    result = {"resource": resource, "filters": query, "items": entries,
              "total": total,
              "page": page, "pageSize": limit, "hasMore": offset + len(entries) < total}
    return base.ok(json.dumps(result, ensure_ascii=False, separators=(",", ":")))


@tool
def list_quota_requests(config: RunnableConfig, status: Status = "pending", page: Page = 1,
                        limit: Limit = 5, item_id: Identifier | None = None) -> object:
    """分页读取额度重置申请，默认待处理5条、最多10条，含总数和理由节选。

    用 item_id 读取一份申请的理由与处理备注（忽略状态筛选），超长内容有截断标记。
    账号已删除时 account 为 null，不代表不限额或用量为零。
    仅读取申请，不批准、不驳回、不重置额度。
    """
    if item_id is not None:
        page, limit, status = 1, 1, "all"
    return _read("quota/requests", config, page, limit, {"status": status, "id": item_id},
                 _quota_entry, detail=item_id is not None)


@tool
def list_dashboard_todos(config: RunnableConfig, done: StrictBool | None = None,
                         page: Page = 1, limit: Limit = 5) -> object:
    """分页读取当前管理员在后台首页记录的个人待办和日程，默认5条、最多10条。

    可按完成状态筛选，返回总数、正文、完成状态与排期；不包含留言、评论或额度申请队列。
    不修改待办。正文不是稳定编号，列表位置也不能用于写入定位。
    """
    return _read("todos", config, page, limit, {"done": done}, _todo_entry)
