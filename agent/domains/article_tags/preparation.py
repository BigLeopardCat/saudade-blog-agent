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
    return "、".join(f"「{sanitize_untrusted(tag['name'], 40)}」" for tag in tags) or "无标签"


def change_summary(plan, intent=None):
    before, after = plan["before"], plan["after"]
    old = {(tag["level"], tag["id"]): tag for tag in before}
    new = {(tag["level"], tag["id"]): tag for tag in after if tag["id"] is not None}
    added = [tag for tag in after if tag["id"] is None or (tag["level"], tag["id"]) not in old]
    removed = [tag for tag in before if (tag["level"], tag["id"]) not in new]
    lines = [f"文章：《{sanitize_untrusted(plan['title'], 255)}》"]
    if added:
        lines.append("添加标签：" + labels(added))
    if removed:
        lines.append("移除标签：" + labels(removed))
    created = [tag for tag in added if tag["id"] is None]
    for level, label in ((1, "一级"), (2, "二级")):
        selected = [tag for tag in created if tag["level"] == level]
        if selected:
            lines.append(f"将新建{label}标签：" + labels(selected))
    if intent and any(tag["level"] == 2 for tag in added) and intent.get("parent_tag"):
        lines.append("所属一级标签：" + sanitize_untrusted(intent["parent_tag"], 40))
    if intent and intent.get("color") and created:
        lines.append("新标签颜色：" + intent["color"])
    return "\n".join(lines)


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
    target = change_summary(current, request)
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
        question="确认修改文章标签？\n" + target,
        target=target,
    )


def tools(read_tools):
    return {**read_tools, prepare_article_tags.name: prepare_article_tags}
