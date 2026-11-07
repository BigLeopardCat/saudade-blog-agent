"""Bounded, authenticated notification queries delegated to the owning service."""

import json
from typing import Annotated
from urllib.parse import urlencode

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt

from agent.principal import is_verified_identity
from tools import base

__all__ = ["list_notifications"]

_Limit = Annotated[StrictInt, Field(ge=1, le=20)]
_Page = Annotated[StrictInt, Field(ge=1, le=10000)]
_Identifier = Annotated[StrictInt, Field(gt=0, le=2_147_483_647)]
_Query = Annotated[str, Field(min_length=1, max_length=120, strict=True)]
_UnreadOnly = Annotated[StrictBool, Field(
    description="true returns unread notifications only; false includes read notifications. "
                "Set true when the user asks which notifications are unread.")]
class _Notification(BaseModel):
    model_config = ConfigDict(strict=True)
    id: Annotated[StrictInt, Field(gt=0, le=2_147_483_647)]
    type: Annotated[str, Field(max_length=40)]
    title: str
    content: str
    contentTruncated: StrictBool
    link: Annotated[str, Field(max_length=1024)] | None
    isRead: StrictBool
    createdAt: Annotated[str, Field(max_length=40)]


class _PagePayload(BaseModel):
    model_config = ConfigDict(strict=True)
    items: Annotated[list[_Notification], Field(max_length=20)]
    unread: Annotated[StrictInt, Field(ge=0)]
    total: Annotated[StrictInt, Field(ge=0)]
    page: Annotated[StrictInt, Field(ge=1)]
    pageSize: Annotated[StrictInt, Field(ge=1, le=20)]
    hasMore: StrictBool
    contentIncluded: StrictBool


class _DetailPayload(BaseModel):
    item: _Notification | None


def _response(data, page, limit, preview, unread_only, notification_id, term):
    if notification_id is not None:
        detail = _DetailPayload.model_validate(data)
        selected = [detail.item.model_dump()] if detail.item else []
        result = {"contentIncluded": True}
        if selected and selected[0]["id"] != notification_id:
            raise ValueError("notification identity mismatch")
    else:
        payload = _PagePayload.model_validate(data)
        selected = [row.model_dump() for row in payload.items]
        if (payload.page != page or payload.pageSize != limit or len(selected) > limit
                or payload.total < len(selected) or payload.contentIncluded is not preview
                or unread_only and any(row["isRead"] for row in selected)):
            raise ValueError("notification scope mismatch")
        result = {"page": page, "pageSize": limit, "total": payload.total,
                  "hasMore": payload.hasMore, "unreadOnly": unread_only,
                  "unreadTotal": payload.unread, "contentIncluded": preview}
        if term is not None:
            result["query"] = term
    if len({row["id"] for row in selected}) != len(selected):
        raise ValueError("duplicate notification identity")
    result["items"] = [_entry(row, notification_id is not None, preview) for row in selected]
    return result


def _entry(row, detailed, include_preview):
    result = {"id": row["id"], "isRead": row["isRead"], "link": row.get("link")}
    fields = [("type", 40), ("title", 160), ("createdAt", 40)]
    if detailed or include_preview:
        fields.append(("content", 2000 if detailed else 240))
    for field, cap in fields:
        result[field] = row[field][:cap]
        if field in ("title", "content"):
            result[field + "Truncated"] = (len(row[field]) > cap or
                                           row.get(field + "Truncated") is True)
    return result


@tool
def list_notifications(config: RunnableConfig, limit: _Limit | None = None, page: _Page = 1,
                       unread_only: _UnreadOnly = False, include_preview: StrictBool = False,
                       notification_id: _Identifier | None = None,
                       query: _Query | None = None) -> object:
    """查询本人的通知；筛选和分页由后端执行，默认不读取正文。

    未读设置unread_only=true；query按原词搜索完整标题和正文。
    默认每页20条；include_preview=true时默认10条、每条最多240字。
    notification_id读取任意历史单条正文（最多2000字），忽略列表条件。
    total是匹配总数，hasMore表示还有下一页，unreadTotal是全局未读数。
    查看不会标记已读；站内信使用list_my_messages。
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
    limit = limit if limit is not None else (10 if include_preview else 20)
    if notification_id is not None:
        path = f"/api/protected/notifications/{notification_id}"
    else:
        params = {"page": page, "pageSize": limit, "unreadOnly": str(unread_only).lower(),
                  "content": "preview" if include_preview else "none"}
        if term is not None:
            params["query"] = term
        path = "/api/protected/notifications?" + urlencode(params)
    data = base._own_get(path, config)
    if isinstance(data, base.ToolResult):
        return data
    try:
        result = _response(data, page, limit, include_preview, unread_only, notification_id, term)
    except ValueError:
        return base.unavailable("通知标识、分页或内容无法核对，请稍后重查。")
    encoded = json.dumps(result, ensure_ascii=False, separators=(",", ":"))
    if len(encoded) > 7500:
        return base.unavailable("通知内容超过读取上限，请减少列表条数或通过站内通知页面查看。")
    return base.ok(encoded, data=result)
