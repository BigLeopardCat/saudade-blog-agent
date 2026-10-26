"""Prepare one exact recipient and the complete notice text for approval."""

import json
from typing import Annotated

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool
from pydantic import Field

from agent.domains.admin_writes import available
from agent.domains.notices import targets
from agent.products.blog.confirmation import proposal
from agent.runtime.turns import check_cancelled
from agent.runtime.writes import bound_context
from tools import base
from tools.result import invalid_args

TOOL = "send_user_notice"
WORKFLOW = "notice_send"


def quoted(value):
    # Reversible quotation prevents original text from becoming a legacy command frame.
    return json.dumps(value, ensure_ascii=False).replace(":", r"\u003a").replace("：", r"\uff1a")


def _prepare(target, title, content, config):
    check_cancelled(config)
    if not available(config):
        return base.unavailable("需要已验签的管理员和稳定会话，本次未生成确认。")
    try:
        request = targets.intent({"target": target, "title": title, "content": content})
    except (TypeError, ValueError) as exc:
        return invalid_args(str(exc))
    plan = targets.prepare(request, config)
    if isinstance(plan, base.ToolResult):
        return plan
    recipient = plan["recipient"]
    target_text = f"发送个人通知给账号 #{recipient['id']} {quoted(recipient['username'])}"
    question = (
        f"{target_text}\n标题：{quoted(plan['title'])}\n正文：\n\n{plan['content']}"
        "\n\n发送后没有撤回入口。确认发送？"
    )
    principal, cid = bound_context(config)
    check_cancelled(config)
    return proposal(
        principal,
        cid,
        WORKFLOW,
        {"tool": TOOL, "args": {"version": plan["version"], "intent": request}},
        executor="notices",
        question=question,
        target=target_text,
    )


@tool(return_direct=True)
def prepare_user_notice(
    content: Annotated[targets.Content, Field(description=(
        "实际发送正文。用户指定原文则逐字保留；代写时，后续待执行操作须用将/计划表述。"
        "例如先通知再降级，应写将调整，不能在没有成功回执时写已调整。"
        "不增添用户未提供的规则、条件或承诺。"))],
    config: RunnableConfig,
    user_id: targets.Identifier | None = None,
    username: targets.Username | None = None,
    title: targets.Title | None = None,
) -> object:
    """准备给单个账号发送个人通知的确认，不写入。收件人以唯一编号或完整账号名定位。
    同时提供user_id和username时必须指向同一个账号。
    正文最多1000字，完整展示后确认；标题可选，最多128字，省略为站内通知。
    代写只使用用户提供或工具核实的信息；群体公开通知应使用公告工具。
    """
    return _prepare({"id": user_id, "username": username}, title, content, config)


def tools(read_tools):
    return {**read_tools, prepare_user_notice.name: prepare_user_notice}
