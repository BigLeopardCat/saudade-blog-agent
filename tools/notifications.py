"""Bounded, authenticated reads of the backend's recent notification window."""

import json
from typing import Annotated

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool
from pydantic import Field, StrictBool, StrictInt

from agent.principal import is_verified_identity
from tools import base

__all__ = ["list_notifications"]

_Limit = Annotated[StrictInt, Field(ge=1, le=10)]
_Page = Annotated[StrictInt, Field(ge=1, le=100)]
_Identifier = Annotated[StrictInt, Field(gt=0)]
_Query = Annotated[str, Field(min_length=1, max_length=120, strict=True)]
_WINDOW_LIMIT = 100


def _window(data):
    rows = data.get("items") if isinstance(data, dict) else None
    if not isinstance(rows, list) or len(rows) > _WINDOW_LIMIT:
        raise ValueError("Invalid notification window")
    for row in rows:
        if (not isinstance(row, dict) or type(row.get("id")) is not int or row["id"] <= 0
                or type(row.get("isRead")) is not bool
                or any(not isinstance(row.get(key), str) for key in
                       ("type", "title", "content", "createdAt"))):
            raise ValueError("Invalid notification fields")
        link = row.get("link")
        if "link" not in row or (link is not None and
                                  (not isinstance(link, str) or len(link) > 1024)):
            raise ValueError("Invalid notification link")
        if len(row["type"]) > 40 or len(row["createdAt"]) > 40:
            raise ValueError("Invalid notification metadata")
    if (len({row["id"] for row in rows}) != len(rows)
            or type(data.get("unread")) is not int
            or data["unread"] != sum(not row["isRead"] for row in rows)):
        raise ValueError("Invalid notification counts")
    return rows, data["unread"]


def _entry(row, detailed):
    result = {"id": row["id"], "isRead": row["isRead"], "link": row.get("link")}
    for field, cap in (("type", 40), ("title", 160),
                       ("content", 2000 if detailed else 240), ("createdAt", 40)):
        result[field] = row[field][:cap]
        if field in ("title", "content"):
            result[field + "Truncated"] = len(row[field]) > cap
    return result


@tool
def list_notifications(config: RunnableConfig, limit: _Limit = 5, page: _Page = 1,
                       unread_only: StrictBool = False,
                       notification_id: _Identifier | None = None,
                       query: _Query | None = None) -> object:
    """读取本人的最近通知及原始跳转链接；默认5条，最多10条，正文节选240字。

    page 翻阅最近100条窗口，unread_only 只筛选该窗口内未读通知。
    notification_id 读取窗口内单条正文（最多2000字），忽略分页和未读筛选。
    query 在整个窗口的标题和正文中按原词搜索，再分页；查回复可搜索“回复”。
    matchedInWindow 是筛选后的匹配总数，hasMoreInWindow=true 时当前页不是全部结果。
    windowComplete=false 表示窗口外历史未知；unreadInWindow 不是全局未读数。
    查看不会标记已读。站内信使用 list_my_messages。
    """
    # Registry construction also occurs during policy imports; enforce after bootstrap.
    from agent.authz import SCOPE_READ_OWN, holds

    cfg = (config or {}).get("configurable") or {}
    principal, uid = cfg.get("principal"), cfg.get("user_id")
    if (principal is None or not is_verified_identity(principal)
            or type(uid) is not int or uid != principal.uid
            or not holds(principal, SCOPE_READ_OWN)):
        return base.unavailable("需要已验签且一致的本人身份，无法读取通知。")
    term = query.strip().casefold() if query is not None and notification_id is None else None
    if term == "":
        return base.unavailable("搜索词不能为空，请提供要查找的标题或正文片段。")
    data = base._own_get("/api/protected/notifications", config)
    if isinstance(data, base.ToolResult):
        return data
    try:
        rows, unread = _window(data)
    except ValueError:
        return base.unavailable("通知窗口、内容或未读数量无法核对，请稍后重查。")
    matched = [row for row in rows if (not unread_only or not row["isRead"])
               and (term is None or any(term in row[field].casefold()
                                        for field in ("title", "content")))]
    if notification_id is not None:
        selected = [row for row in rows if row["id"] == notification_id]
    else:
        selected = matched[(page - 1) * limit:page * limit]
    result = {"items": [_entry(row, notification_id is not None) for row in selected],
              "windowLimit": _WINDOW_LIMIT, "windowCount": len(rows),
              "windowComplete": len(rows) < _WINDOW_LIMIT, "unreadInWindow": unread}
    if notification_id is None:
        result.update(page=page, pageSize=limit, matchedInWindow=len(matched),
                      hasMoreInWindow=page * limit < len(matched))
        if query is not None:
            result["query"] = query.strip()
    encoded = json.dumps(result, ensure_ascii=False, separators=(",", ":"))
    if len(encoded) > 7500:
        return base.unavailable("通知内容超过读取上限，请减少列表条数或通过站内通知页面查看。")
    return base.ok(encoded, data=result)
