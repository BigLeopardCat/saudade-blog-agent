"""Bounded administrator quota requests and personal dashboard todos."""

from datetime import date
from functools import partial
from typing import Literal

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool
from pydantic import StrictBool

from agent.domains.admin_pages import Identifier, Limit, Page, read_page
from agent.reports import sanitize_untrusted

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


def _todo_entry(row, detail):
    if (not isinstance(row, dict) or not isinstance(row.get("text"), str)
            or not row["text"].strip() or len(row["text"]) > 200
            or type(row.get("done")) is not bool or "date" not in row):
        raise ValueError("Invalid todo")
    due = row["date"]
    if due is not None and (not isinstance(due, str) or date.fromisoformat(due).isoformat() != due):
        raise ValueError("Invalid due date")
    return {"text": sanitize_untrusted(row["text"], 0), "done": row["done"], "date": due}


def _read(resource, config, page, limit, query, entry, detail=False):
    expected = {key: value for key, value in query.items()
                if value is not None and not (key == "status" and value == "all")}
    return read_page(resource, config, page, limit, query, partial(entry, detail=detail), expected)


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
