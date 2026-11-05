"""Prepare state-only article changes without writing business data."""

from typing import Annotated, Literal

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool
from pydantic import Field, StrictInt

from agent.domains.article_actions import batch, targets
from agent.proposals import proposal
from agent.reports import sanitize_untrusted
from agent.runtime.results import TurnResult
from agent.runtime.turns import check_cancelled
from agent.runtime.writes import bound_context
from agent.scheduling.models import WriteSchedule
from tools import base
from tools.result import invalid_args

available = targets.available
Identifier = Annotated[StrictInt, Field(gt=0)]
ArticleIds = Annotated[list[Identifier], Field(min_length=1, max_length=50)]


@tool(return_direct=True)
def prepare_article_state(
    config: RunnableConfig,
    article_id: Identifier | None = None,
    article_ids: ArticleIds | None = None,
    category: Annotated[str, Field(min_length=1, max_length=100)] | None = None,
    status: Literal["public", "private", "draft"] | None = None,
    is_top: Annotated[StrictInt, Field(ge=0, le=1)] | None = None,
    schedule: WriteSchedule | None = None,
) -> object:
    """按明确请求准备文章发布、隐藏、转草稿或置顶确认；只改指定字段，不编辑正文、署名或修改稿。

    单篇传article_id；整批传article_ids或用户给定的完整分类名category，三者仅选一种。
    一次可准备最多50篇，整批核对版本、展示范围并只确认一次；超出范围明确报错，不截断。
    编号与分类直接取用户给定信息，本工具负责读取状态和核实目标，不编辑正文。
    本站public才公开可读；private和draft不进入公开列表、搜索或公开正文接口，管理员仍可后台查询。
    发布当前打开的编辑稿请用article_editor领域的prepare_article_publication；本工具不应用修改稿。
    状态修改保留文章编号、正文、标签和互动记录，恢复public沿用原链接；不撤回外部已保存的副本。
    """
    check_cancelled(config)
    if not available(config):
        return base.unavailable("需要已验签的管理员和稳定会话，本次未生成确认卡。")
    if status is None and is_top is None:
        return invalid_args("请明确要改的文章状态或置顶开关。")
    if sum(value is not None for value in (article_id, article_ids, category)) != 1:
        return invalid_args("请选择单篇编号、批量编号或一个完整分类名。")
    if article_ids is not None or category is not None:
        return batch.prepare(article_ids, category, status, is_top, config, schedule=schedule)
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
    if schedule is None and targets.satisfied(current, args):
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
        schedule=schedule,
    )


def tools(read_tools):
    return {**read_tools, prepare_article_state.name: prepare_article_state}
