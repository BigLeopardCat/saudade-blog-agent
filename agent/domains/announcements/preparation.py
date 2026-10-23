"""Prepare complete announcement text and notification impact for approval."""

import json

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool

from agent.domains.admin_writes import available
from agent.domains.announcements import targets
from agent.reports import sanitize_untrusted
from agent.runtime.confirmation import proposal
from agent.runtime.results import TurnResult
from agent.runtime.turns import check_cancelled
from agent.runtime.writes import bound_context
from tools import base
from tools.result import invalid_args

TOOLS = {
    "create": "create_announcement",
    "update": "update_announcement",
    "delete": "delete_announcement",
}
WORKFLOWS = {key: "announcement_" + key for key in TOOLS}


def _quoted_content(value):
    # A reversible JSON string keeps whitespace visible and cannot form a browser command frame.
    return json.dumps(value, ensure_ascii=False).replace(":", r"\u003a").replace("：", r"\uff1a")


def description(plan):
    before, after = plan["before"], plan["after"]
    row = before or after
    heading = f"公告 #{row['id']}" if row["id"] else "新公告"
    if after is None:
        details = "删除「" + sanitize_untrusted(before["title"], 80) + "」及其关联通知"
    else:
        lines = []
        for field, label in (("title", "标题"), ("content", "正文")):
            if before is None or before[field] != after[field]:
                render = (
                    _quoted_content
                    if field == "content"
                    else lambda value: sanitize_untrusted(value, 80)
                )
                old = render(before[field]) + " → " if before else ""
                suffix = "（JSON原文，\\n表示换行）" if field == "content" else ""
                lines.append(label + suffix + "：" + old + render(after[field]))
        details = "\n".join(lines) or "内容已达成，无需改动"
    scope = "创建" if before is None else "删除" if after is None else "同步更新"
    return f"{heading}：{details}\n{scope} {plan['notificationCount']} 条关联个人通知；公告公开可见"


def _prepare(action, target, fields, config):
    check_cancelled(config)
    if not available(config):
        return base.unavailable("需要已验签的管理员和稳定会话，本次未生成确认卡。")
    try:
        request = targets.intent({"action": action, "target": target, "fields": fields})
    except (ValueError, TypeError) as exc:
        return invalid_args(str(exc))
    plan = targets.prepare(request, config)
    if isinstance(plan, base.ToolResult):
        return plan
    spec = {"tool": TOOLS[action], "args": {"version": plan["version"], "intent": request}}
    text = description(plan)
    if plan["before"] == plan["after"]:
        return TurnResult(
            text, {"noop_note": text, "noop_text": text, "task_satisfied": [spec], "receipts": []}
        )
    principal, cid = bound_context(config)
    check_cancelled(config)
    return proposal(
        principal,
        cid,
        WORKFLOWS[action],
        spec,
        executor="announcements",
        question=text + "。确认执行？",
        target=text,
    )


@tool(return_direct=True)
def prepare_announcement_create(
    title: targets.Title, content: targets.Content, config: RunnableConfig
) -> object:
    """准备发布站内公告确认，不写入。确认卡完整展示标题和正文；用户给出原文时照录。
    公告公开可见，并为当前用户创建个人通知，点击确认后原子发布。
    """
    return _prepare("create", None, {"title": title, "content": content}, config)


@tool(return_direct=True)
def prepare_announcement_update(
    config: RunnableConfig,
    announcement_id: targets.Identifier | None = None,
    announcement_title: targets.Title | None = None,
    new_title: targets.Title | None = None,
    content: targets.Content | None = None,
) -> object:
    """准备修改公告确认，不写入。以唯一编号或完整原标题定位；仅填写需改的标题或正文。
    保留未指定字段，并同步关联个人通知；不重复发布公告，不增加通知接收人。
    """
    return _prepare(
        "update",
        {"id": announcement_id, "title": announcement_title},
        {"title": new_title, "content": content},
        config,
    )


@tool(return_direct=True)
def prepare_announcement_delete(
    config: RunnableConfig,
    announcement_id: targets.Identifier | None = None,
    announcement_title: targets.Title | None = None,
) -> object:
    """准备删除公告确认，不写入。以唯一编号或完整原标题定位，同时删除其关联个人通知。"""
    return _prepare("delete", {"id": announcement_id, "title": announcement_title}, {}, config)


def tools(read_tools):
    return {
        **read_tools,
        **{
            item.name: item
            for item in (
                prepare_announcement_create,
                prepare_announcement_update,
                prepare_announcement_delete,
            )
        },
    }
