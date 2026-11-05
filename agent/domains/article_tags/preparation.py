"""Prepare one article tag change; the service creates nothing until approval."""

from typing import Annotated

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool
from pydantic import Field

from agent.domains.admin_writes import available
from agent.domains.article_tags import batch, targets
from agent.domains.article_tags.presentation import change_summary
from agent.proposals import proposal
from agent.runtime.results import TurnResult
from agent.runtime.turns import check_cancelled
from agent.runtime.writes import bound_context
from agent.scheduling.models import WriteSchedule
from tools import base
from tools.result import invalid_args


@tool(return_direct=True)
def prepare_article_tags(
    config: RunnableConfig,
    article_id: targets.Identifier | None = None,
    article_ids: Annotated[list[targets.Identifier], Field(min_length=1, max_length=20)]
    | None = None,
    add: targets.Names | None = None,
    remove: targets.Names | None = None,
    replace: targets.Names | None = None,
    level: targets.Level | None = None,
    parent_tag: targets.Name | None = None,
    color: str | None = None,
    schedule: WriteSchedule | None = None,
) -> object:
    """准备文章标签增删或整体替换确认，不写入。add/remove填写用户要操作的标签名列表。

    单篇传article_id，同类整批传article_ids，二者选一；最多20篇，整批只确认一次。
    输入用户给定的编号和标签名，本工具整批核实版本、范围及新建计划，无需逐篇查接口。
    每次合计至多10个标签名。未指定的标签保留；仅明确整体替换时传replace，[]表示清空。
    同名不唯一时用level或parent_tag区分。新增缺失标签在确认后与挂载同一事务完成；
    默认建一级，建二级需已有父标签名。color仅用于新建，格式#RRGGBB；不传使用应用默认。
    """
    check_cancelled(config)
    if not available(config):
        return base.unavailable("需要已验签的管理员和稳定会话，本次未生成确认卡。")
    try:
        request = targets.intent(
            {
                "add": add or [],
                "remove": remove or [],
                "replace": replace,
                "level": level,
                "parent_tag": parent_tag,
                "color": color,
            }
        )
    except (ValueError, TypeError) as exc:
        return invalid_args(str(exc))
    if (article_id is None) == (article_ids is None):
        return invalid_args("请选择单篇编号或一组文章编号，不能混用。")
    if article_ids is not None:
        return batch.prepare(article_ids, request, config, schedule=schedule)
    current = targets.prepare(article_id, request, config)
    if isinstance(current, base.ToolResult):
        return current
    args = {"article_id": article_id, "version": current["version"], "intent": request}
    spec = {"tool": "set_article_tags", "args": args}
    target = change_summary(current, request)
    if schedule is None and current["before"] == current["after"]:
        text = target + "，标签已达成，本次无需改动。"
        return TurnResult(
            text, {"noop_note": text, "noop_text": text, "task_satisfied": [spec], "receipts": []}
        )
    principal, cid = bound_context(config)
    check_cancelled(config)
    return proposal(
        principal,
        cid,
        "article_tags",
        spec,
        executor="article_tags",
        question="确认修改文章标签？\n" + target,
        target=target,
        schedule=schedule,
    )


def tools(read_tools):
    return {**read_tools, prepare_article_tags.name: prepare_article_tags}
