"""Prepare one article tag change; the service creates nothing until approval."""

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool

from agent.domains.admin_writes import available
from agent.domains.article_tags import targets
from agent.reports import sanitize_untrusted
from agent.runtime.confirmation import proposal
from agent.runtime.results import TurnResult
from agent.runtime.turns import check_cancelled
from agent.runtime.writes import bound_context
from tools import base
from tools.result import invalid_args


def labels(tags):
    parts = []
    for tag in tags:
        label = sanitize_untrusted(tag["name"], 40)
        hierarchy = (
            f"一级 #{tag['id']}"
            if tag["level"] == 1
            else f"二级 #{tag['id']}，父级 #{tag['parentId']}"
        )
        parts.append(
            f"「{label}」（{hierarchy}）"
            if tag["id"]
            else f"「{label}」（新建{'一级' if tag['level'] == 1 else '二级'}"
            + (f"，父级 #{tag['parentId']}" if tag["parentId"] else "")
            + (f"，颜色 {tag['color']}" if tag["color"] else "")
            + "）"
        )
    return "、".join(parts) or "无标签"


@tool(return_direct=True)
def prepare_article_tags(
    article_id: targets.Identifier,
    config: RunnableConfig,
    add: targets.Names | None = None,
    remove: targets.Names | None = None,
    replace: targets.Names | None = None,
    level: targets.Level | None = None,
    parent_tag: targets.Name | None = None,
    color: str | None = None,
) -> object:
    """准备文章标签增删或整体替换确认，不写入。add/remove填写用户要操作的标签名列表。

    先核对文章编号，每次合计至多10个标签名。未指定的标签保留；仅明确整体替换时传replace，[]表示清空。
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
    current = targets.prepare(article_id, request, config)
    if isinstance(current, base.ToolResult):
        return current
    args = {"article_id": article_id, "version": current["version"], "intent": request}
    spec = {"tool": "set_article_tags", "args": args}
    title = sanitize_untrusted(current["title"], 255)
    target = (
        f"文章 {article_id}《{title}》：{labels(current['before'])} → {labels(current['after'])}"
    )
    if current["before"] == current["after"]:
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
        question=target + "？",
        target=target,
    )


def tools(read_tools):
    return {**read_tools, prepare_article_tags.name: prepare_article_tags}
