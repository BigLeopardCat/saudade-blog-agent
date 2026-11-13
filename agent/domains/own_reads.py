"""Bounded current-account counts and message reads."""

import json
from typing import Annotated, Literal
from urllib.parse import urlencode

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr, model_validator

from agent.runtime.access import own_reads_enabled
from tools import base

_FIELDS = ("id", "fromUserId", "toUserId", "peerName", "title", "isRead", "createdAt")


def _principal(config):
    principal = ((config or {}).get("configurable") or {}).get("principal")
    uid = ((config or {}).get("configurable") or {}).get("user_id")
    if (not own_reads_enabled(principal)
            or (uid is not None and (type(uid) is not int or uid != principal.uid))):
        return None
    return principal


def _render(data):
    return base.ok(json.dumps(data, ensure_ascii=False, separators=(",", ":")), data=data)


def _entry(row, uid, folder=None):
    if (not isinstance(row, dict) or type(row.get("id")) is not int or row["id"] <= 0
            or type(row.get("isRead")) is not bool):
        return None
    owners = ("toUserId",) if folder == "inbox" else (
        ("fromUserId",) if folder == "outbox" else ("toUserId", "fromUserId"))
    if not any(type(row.get(key)) is int and row[key] == uid for key in owners):
        return None
    return {key: row[key] for key in _FIELDS if key in row}


Count = Annotated[StrictInt, Field(ge=0)]
PreviewLimit = Annotated[StrictInt, Field(ge=1, le=5)]


class Header(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: Annotated[StrictInt, Field(gt=0)]
    title: Annotated[StrictStr, Field(max_length=64)]
    titleTruncated: StrictBool
    createdAt: StrictStr
    isRead: StrictBool

    @model_validator(mode="after")
    def unread(self):
        if self.isRead:
            raise ValueError("Overview contains a read item")
        return self


class NotificationHeader(Header):
    type: Annotated[StrictStr, Field(max_length=40)]


class MessageHeader(Header):
    fromUserId: Annotated[StrictInt, Field(gt=0)]
    peerName: Annotated[StrictStr, Field(max_length=80)]


class InboxOverview(BaseModel):
    userId: Annotated[StrictInt, Field(gt=0)]
    notifications: Count
    messages: Count
    total: Count
    limit: PreviewLimit
    unreadOnly: StrictBool
    notificationItems: list[NotificationHeader]
    messageItems: list[MessageHeader]
    notificationsHasMore: StrictBool
    messagesHasMore: StrictBool

    @model_validator(mode="after")
    def complete(self):
        if self.total != self.notifications + self.messages or not self.unreadOnly:
            raise ValueError("Inconsistent unread counts")
        for rows, count, more in ((self.notificationItems, self.notifications,
                                   self.notificationsHasMore),
                                  (self.messageItems, self.messages, self.messagesHasMore)):
            if (len(rows) != min(count, self.limit) or more != (len(rows) < count)
                    or len({row.id for row in rows}) != len(rows)):
                raise ValueError("Incomplete unread preview")
        return self


@tool
def get_inbox_overview(config: RunnableConfig, limit: PreviewLimit = 5,
    mode: Annotated[Literal["recent_unread", "counts_only"], Field(description=
        "recent_unread lists unread headers and counts; use it for which/what unread items. "
        "counts_only is only for an explicitly count-only request.")] = "recent_unread") -> object:
    """查看本人未读通知与站内信：可靠计数和最近各5条未读标题（含公告副本），只读不标记。

    只查未读，不覆盖最近全部通知；查最近通知直接加载notifications领域。hasMore为true时明确仅展示最近部分。
    仅明确要求只查数量时选择counts_only；更多条目、正文和操作使用notifications、messages领域。
    """
    principal = _principal(config)
    if principal is None:
        return base.unavailable("需要已验签的本人身份，无法查询未读收件。")
    if mode == "counts_only":
        data = base._own_get("/api/protected/notifications/summary", config)
        if isinstance(data, base.ToolResult):
            return data
        fields = ("notifications", "messages", "total")
        if (not isinstance(data, dict)
                or any(type(data.get(key)) is not int or data[key] < 0 for key in fields)
                or data["total"] != data["notifications"] + data["messages"]):
            return base.unavailable("未能核对未读数量，本次不作结论。")
        return _render({**{key: data[key] for key in fields}, "itemsIncluded": False})
    data = base._own_get("/api/protected/inbox/overview?" + urlencode({"limit": limit}), config)
    if isinstance(data, base.ToolResult):
        return data
    try:
        result = InboxOverview.model_validate(data)
        if result.userId != principal.uid or result.limit != limit:
            raise ValueError("Inbox recipient or preview scope mismatch")
    except ValueError:
        return base.unavailable("未能核对未读计数和条目，本次不作结论。")
    return _render({**result.model_dump(exclude={"userId"}), "itemsIncluded": True})


@tool
def list_my_messages(
    config: RunnableConfig,
    folder: Literal["inbox", "outbox"] = "inbox",
    limit: Annotated[StrictInt, Field(ge=1, le=10)] = 5,
    message_id: Annotated[StrictInt, Field(gt=0)] | None = None,
) -> object:
    """查看本人最近信件的标题、对方、时间和已读状态，默认收件箱5封，最多10封。

    列表不含正文，也不代表整个信箱。需要正文时提供已有编号 message_id，读取单封。
    查询不会标记已读；收件箱与发件箱分开查询。
    """
    principal = _principal(config)
    if principal is None:
        return base.unavailable("需要已验签的本人身份，无法查询信件。")
    path = (f"/api/protected/messages/{message_id}" if message_id is not None else
            "/api/protected/messages/recent?" + urlencode({"folder": folder, "limit": limit}))
    data = base._own_get(path, config)
    if isinstance(data, base.ToolResult):
        return data
    if not isinstance(data, dict):
        return base.unavailable("未能核对信件返回结果。")
    if message_id is not None:
        entry = _entry(data, principal.uid)
        if (entry is None or entry["id"] != message_id
                or not isinstance(data.get("content"), str)):
            return base.unavailable("未能核对这封信的归属与内容。")
        return _render({**entry, "content": data["content"]})
    items = data.get("items")
    if (not isinstance(items, list) or len(items) > limit
            or data.get("folder") != folder or type(data.get("hasMore")) is not bool):
        return base.unavailable("未能核对最近信件列表，请稍后重查。")
    entries = [_entry(row, principal.uid, folder) for row in items]
    if any(entry is None for entry in entries):
        return base.unavailable("信件列表包含无法核对的归属或状态，本次不作结论。")
    return _render({"scope": "recent_window", "folder": folder, "items": entries,
                    "limit": limit, "returnedCount": len(entries),
                    "collectionComplete": not data["hasMore"],
                    "hasMore": data["hasMore"], "unreadTotal": None})
