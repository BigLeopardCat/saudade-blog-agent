"""Prepare state-only article changes without writing business data."""

from typing import Annotated, Literal

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool
from pydantic import Field, StrictInt

from agent.domains.article_actions import targets
from agent.reports import sanitize_untrusted
from agent.runtime.confirmation import proposal
from agent.runtime.results import TurnResult
from agent.runtime.turns import check_cancelled
from agent.runtime.writes import bound_context
from tools import base
from tools.result import invalid_args

available = targets.available


@tool(return_direct=True)
def prepare_article_state(
    article_id: Annotated[StrictInt, Field(gt=0)],
    config: RunnableConfig,
    status: Literal["public", "private", "draft"] | None = None,
    is_top: Annotated[StrictInt, Field(ge=0, le=1)] | None = None,
) -> object:
    """按明确请求准备文章发布、隐藏、转草稿或置顶确认；只改指定字段，不编辑正文、署名或修改稿。

    article_id来自真实后台清单或明确编号。核对当前文章与内容版本后生成确认卡，不执行写入。
    """
    check_cancelled(config)
    if not available(config):
        return base.unavailable("需要已验签的管理员和稳定会话，本次未生成确认卡。")
    if status is None and is_top is None:
        return invalid_args("请明确要改的文章状态或置顶开关。")
    current = targets.read(article_id, config)
    if isinstance(current, base.ToolResult):
        return current
    args = {
        "article_id": article_id,
        "version": current["version"],
        "status": status,
        "is_top": is_top,
    }
    spec = {"tool": "set_article_status", "args": args}
    title = sanitize_untrusted(current["title"], 255)
    target = f"文章 {article_id}《{title}》：{targets.description(args)}"
    if targets.satisfied(current, args):
        text = target + "，当前状态已达成，本次无需改动。"
        return TurnResult(
            text, {"noop_note": text, "noop_text": text, "task_satisfied": [spec], "receipts": []}
        )
    principal, cid = bound_context(config)
    check_cancelled(config)
    return proposal(
        principal,
        cid,
        "set_status",
        spec,
        executor="article_state",
        question=target + "？",
        target=target,
    )


def tools(read_tools):
    return {**read_tools, prepare_article_state.name: prepare_article_state}
