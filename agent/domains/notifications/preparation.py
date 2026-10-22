"""Resolve an explicit notification selection into a fixed confirmation scope."""

from typing import Annotated, Literal

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool
from pydantic import Field, StrictInt

from agent import authz
from agent.runtime.confirmation import proposal
from agent.runtime.results import TurnResult
from agent.runtime.turns import check_cancelled
from agent.runtime.writes import bound_context
from tools import base
from tools.own_data import _note_items


def available(config):
    try:
        principal, _ = bound_context(config)
    except ValueError:
        return False
    return authz.check(principal, "read_notifications").allowed


def snapshot(config):
    data = base._own_get("/api/protected/notifications", config)
    if isinstance(data, base.ToolResult):
        return None, data
    rows = _note_items(data)
    if rows is None or any(type(row.get("isRead")) is not bool for row in rows.values()):
        return None, base.unavailable("未能核对通知及其已读状态，本次未改动。")
    return (data, rows), None


@tool(return_direct=True)
def prepare_notification_read(
    scope: Literal["selected", "all_unread"],
    config: RunnableConfig,
    ids: Annotated[list[Annotated[StrictInt, Field(gt=0)]], Field(max_length=100)] = [],
) -> object:
    """用户要求将本人通知标记已读时准备确认卡，不写入。

    selected 需具体编号；all_unread 仅用于用户明确要求全部未读。
    """
    check_cancelled(config)
    if not available(config):
        return base.unavailable("需要已验签的本人身份和稳定会话，本次未生成确认卡。")
    if (scope == "selected" and not ids) or (scope == "all_unread" and ids):
        return base.unavailable("请明确通知范围：提供具体通知编号，或选择全部未读。")
    value, error = snapshot(config)
    if error is not None:
        return error
    data, rows = value
    selected = sorted(set(ids)) if scope == "selected" else sorted(
        key for key, row in rows.items() if not row["isRead"])
    if any(key not in rows for key in selected):
        return base.unavailable("部分通知编号不在本人可核对的列表中，请核对编号；本次未改动。")
    unread = [key for key in selected if not rows[key]["isRead"]]
    if scope == "all_unread" and (
            type(data.get("unread")) is not int or data["unread"] != len(unread)):
        return base.unavailable("列表未完整覆盖全部未读通知，请指定具体编号，本次未生成确认卡。")
    if not unread:
        text = "所选通知均已读，本次无需改动。"
        return TurnResult(text, {"noop_note": text, "noop_text": text, "receipts": []})
    labels = [f"{key}《{str(rows[key].get('title') or '无标题通知')[:80]}》" for key in unread]
    target = "将通知 " + "、".join(labels) + " 标记为已读"
    question = target + "？已读不可撤销；确认后新到的通知不包含在内。"
    if len(question) > 500:
        return base.unavailable("所选通知过多，无法完整展示确认范围，请分批选择。")
    principal, cid = bound_context(config)
    check_cancelled(config)
    return proposal(principal, cid, "notice_read", {
        "tool": "read_notifications", "args": {"ids": unread, "all": False}},
        executor="notifications", question=question, target=target,
        reply=question + "\n点「确定」后才会处理，也可以告诉我修改或取消。")


def tools(own_tools):
    return {"list_notifications": own_tools["list_notifications"],
            prepare_notification_read.name: prepare_notification_read}
