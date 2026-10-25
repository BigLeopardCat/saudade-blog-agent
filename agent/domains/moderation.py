"""Bounded review lists and aggregate counts; no moderation writes."""

import json
from typing import Annotated, Literal
from urllib.parse import urlencode

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool
from pydantic import Field, StrictInt

from agent.authz import SCOPE_ADMIN_CONSOLE
from agent.products.blog.read_tools import scoped_reads_enabled
from agent.reports import sanitize_untrusted
from tools import base

Status = Literal["pending", "passed", "rejected"]
AiDecision = Literal["pass", "flag", "reject", "none"]
Page = Annotated[StrictInt, Field(ge=1, le=10000)]
Limit = Annotated[StrictInt, Field(ge=1, le=10)]
Identifier = Annotated[StrictInt, Field(gt=0)]
Keyword = Annotated[str, Field(max_length=100)]
_COUNTS = ("total", "pending", "passed", "rejected", "aiPassed", "aiFlagged",
           "aiRejected", "aiUnreviewed")


def _allowed(config):
    cfg = (config or {}).get("configurable") or {}
    return scoped_reads_enabled(cfg.get("principal"), SCOPE_ADMIN_CONSOLE, cfg.get("user_id"))


def _render(data):
    return base.ok(json.dumps(data, ensure_ascii=False, separators=(",", ":")))


def _text(value, limit=80):
    return sanitize_untrusted(value, limit) if isinstance(value, str) and value else None


def _entry(row, kind, detail):
    if not isinstance(row, dict):
        return None
    identifier = row.get("talkKey" if kind == "board" else "id")
    status, content = row.get("approved"), row.get("content")
    if (type(identifier) is not int or identifier <= 0 or type(status) is not int
            or status not in (0, 1, 2) or not isinstance(content, str)):
        return None
    uid = row.get("userId")
    ai_key = "ai_result" if kind == "board" else "aiResult"
    ai = row.get(ai_key)
    if ai_key not in row or ai not in (None, "pass", "flag", "reject"):
        return None
    if kind == "comments" and (type(row.get("isDeleted")) is not int
                               or row["isDeleted"] not in (0, 1)
                               or type(row.get("noteId")) is not int or row["noteId"] <= 0):
        return None
    entry = {"id": identifier, "status": ("pending", "passed", "rejected")[status],
             "publisher": {"id": uid if type(uid) is int and uid > 0 else None,
                           "username": _text(row.get("username")),
                           "nickname": _text(row.get("nickname"))},
             "createdAt": row.get("createTime"),
             "aiDecision": ai,
             "rejectReason": _text(row.get("rejectReason"), 200),
             "aiReason": _text(row.get("aiReason"), 200)}
    if kind == "board":
        entry["displayName"] = _text(row.get("author"))
    else:
        entry.update({"articleId": row.get("noteId"), "articleTitle": _text(row.get("noteTitle")),
                      "isDeleted": row.get("isDeleted")})
    body = _text(content, 0 if detail else 240) if content else ""
    entry["content" if detail else "contentPreview"] = body
    entry["contentTruncated"] = not detail and len(content) > 240
    return entry


def _matches(entries, status, item_id, filters):
    expected = {key: value for key, value in {"id": item_id, "status": status,
                "articleId": filters.get("note_id")}.items() if value is not None}
    ai = filters.get("ai_result")
    if ai is not None:
        expected["aiDecision"] = None if ai == "none" else ai
    return all(all(entry.get(key) == value for key, value in expected.items()) for entry in entries)


def _page(kind, config, status, keyword, page, limit, item_id, **filters):
    if not _allowed(config):
        return base.unavailable("需要已验签的管理员身份，无法读取审核记录。")
    if item_id is not None:
        page, limit = 1, 1
    query = {"page": page, "page_size": limit, "id": item_id, "status": status,
             "q": keyword, **filters}
    query = {key: value for key, value in query.items() if value is not None}
    data = base._admin_get("/api/protect/" + kind + "?" + urlencode(query), config, paginated=True)
    if isinstance(data, base.ToolResult):
        return data
    if not isinstance(data, dict):
        return base.unavailable("审核列表返回格式无法核对。")
    rows, total = data.get("items"), data.get("total")
    offset = (page - 1) * limit
    if (not isinstance(rows, list) or len(rows) > limit or type(total) is not int or total < 0
            or (rows and offset + len(rows) > total)):
        return base.unavailable("审核列表缺少可靠的分页总数或超出本页范围。")
    entries = [_entry(row, kind, item_id is not None) for row in rows]
    if any(entry is None for entry in entries):
        return base.unavailable("审核记录的编号、状态或内容无法核对。")
    if not _matches(entries, status, item_id, filters):
        return base.unavailable("返回记录与请求的编号、审核状态、AI判定或文章范围不一致。")
    return _render({"resource": kind, "filters": query, "items": entries, "total": total,
                    "page": page, "pageSize": limit, "hasMore": offset + len(rows) < total})


@tool
def get_moderation_status(config: RunnableConfig) -> object:
    """查询河灯留言的审核总数与人工状态、AI判定分布，仅取数据库计数。

    AI判定和最终审核状态是两个独立维度；查看具体记录使用 list_admin_board。
    """
    if not _allowed(config):
        return base.unavailable("需要已验签的管理员身份，无法读取审核汇总。")
    data = base._admin_get("/api/protected/moderation/summary", config)
    if isinstance(data, base.ToolResult):
        return data
    if (not isinstance(data, dict) or any(
            type(data.get(key)) is not int or data[key] < 0 for key in _COUNTS)
            or data["pending"] + data["passed"] + data["rejected"] != data["total"]
            or sum(data[key] for key in _COUNTS[4:]) != data["total"]):
        return base.unavailable("审核汇总缺少有效计数或分布与总数不一致。")
    return _render({key: data[key] for key in _COUNTS})


@tool
def list_admin_board(config: RunnableConfig, status: Status | None = None,
                     ai_result: AiDecision | None = None, keyword: Keyword | None = None,
                     page: Page = 1, limit: Limit = 5, item_id: Identifier | None = None) -> object:
    """分页查看河灯留言，默认5条、最多10条，返回总数与内容节选。

    查询某条留言或匿名留言是谁发的，用 item_id 读取 publisher 的账号编号、用户名及昵称。
    displayName 是自由留名，可留空或与账号不同，不能用来认定发表账号。
    status 是最终审核状态，ai_result 是独立的AI判定。提供 item_id 单独读取内容。
    """
    return _page("board", config, status, keyword, page, limit, item_id, ai_result=ai_result)


@tool
def list_admin_comments(config: RunnableConfig, status: Status | None = None,
                        keyword: Keyword | None = None, article_id: Identifier | None = None,
                        page: Page = 1, limit: Limit = 5,
                        item_id: Identifier | None = None) -> object:
    """分页查看文章评论，默认5条、最多10条；可按审核状态、正文或账号关键词、文章编号筛选。

    返回总数、内容节选和软删除标记；评论与河灯留言不同。提供 item_id 单独读取内容。
    """
    return _page("comments", config, status, keyword, page, limit, item_id, note_id=article_id)
