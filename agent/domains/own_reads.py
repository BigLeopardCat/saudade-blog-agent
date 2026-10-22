"""Small own-data views for the native runtime; legacy tool contracts stay intact."""

import json
from typing import Annotated, Literal
from urllib.parse import urlencode

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool
from pydantic import Field, StrictInt

from agent.runtime.read_tools import own_reads_enabled
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
    return base.ok(json.dumps(data, ensure_ascii=False, separators=(",", ":")))


def _entry(row, uid, folder=None):
    if (not isinstance(row, dict) or type(row.get("id")) is not int or row["id"] <= 0
            or type(row.get("isRead")) is not bool):
        return None
    owners = ("toUserId",) if folder == "inbox" else (
        ("fromUserId",) if folder == "outbox" else ("toUserId", "fromUserId"))
    if not any(type(row.get(key)) is int and row[key] == uid for key in owners):
        return None
    return {key: row[key] for key in _FIELDS if key in row}


@tool
def get_unread_summary(config: RunnableConfig) -> object:
    """查询本人的未读通知数、未读站内信数及合计；只取计数，不读取内容。"""
    if _principal(config) is None:
        return base.unavailable("需要已验签的本人身份，无法查询未读数。")
    data = base._own_get("/api/protected/notifications/summary", config)
    if isinstance(data, base.ToolResult):
        return data
    fields = ("notifications", "messages", "total")
    if (not isinstance(data, dict) or any(
            type(data.get(key)) is not int or data[key] < 0 for key in fields)
            or data["total"] != data["notifications"] + data["messages"]):
        return base.unavailable("未能核对未读计数，请稍后重查。")
    return _render({key: data[key] for key in fields})


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
    return _render({"folder": folder, "items": entries, "hasMore": data["hasMore"]})
