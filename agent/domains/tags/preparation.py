"""Prepare a dictionary change without mutating tags or article references."""

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool

from agent.domains.admin_writes import available
from agent.domains.tags import targets
from agent.reports import sanitize_untrusted
from agent.runtime.confirmation import proposal
from agent.runtime.results import TurnResult
from agent.runtime.turns import check_cancelled
from agent.runtime.writes import bound_context
from tools import base
from tools.result import invalid_args

TOOLS = {"create": "create_tag", "update": "update_tag", "delete": "delete_tag"}
WORKFLOWS = {key: "tag_" + key for key in TOOLS}


def _row(row):
    name = sanitize_untrusted(row["name"], 255)
    identity = f"#{row['id']}" if row["id"] else "（执行后分配编号）"
    return f"{row['level']}级标签{identity}「{name}」，颜色{row['color'] or '未设置'}"


def description(plan):
    before, after, parent = plan["before"], plan["after"], plan["parent"]
    if after is None:
        text = f"删除{_row(before)}及其 {plan['childCount']} 个二级子标签；文章不删除，清除对应引用"
    else:
        text = (f"{_row(before)} → " if before else "新建") + _row(after)
        if parent:
            text += f"；父级为{_row(parent)}"
        elif before and before["parentId"]:
            text += "；移除父级，成为一级标签"
        if before and before["level"] != after["level"]:
            text += "；保留文章引用，标签编号可能改变"
    return (f"{text}。影响 {plan['articleCount']} 篇文章（含私密及独立草稿）、"
            f"{plan['revisionCount']} 份编辑草稿")


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
        executor="tags",
        question=text + "。确认执行？",
        target=text,
    )


def _target(tag_id, name, level, parent_id=None):
    return {"id": tag_id, "name": name, "level": level, "parentId": parent_id}


@tool(return_direct=True)
def prepare_tag_create(
    name: targets.Name,
    config: RunnableConfig,
    color: targets.Color | None = None,
    parent_id: targets.Identifier | None = None,
    parent_name: targets.TargetName | None = None,
) -> object:
    """准备新建标签确认，不写入。默认一级；填写一级父标签编号或完整名字则创建二级。
    颜色可选#RRGGBB；这是标签字典管理，给文章加标签应使用文章标签工具。
    """
    parent = (
        _target(parent_id, parent_name, 1)
        if parent_id is not None or parent_name is not None
        else None
    )
    return _prepare("create", None, {"name": name, "color": color, "parent": parent}, config)


@tool(return_direct=True)
def prepare_tag_update(
    config: RunnableConfig,
    tag_id: targets.Identifier | None = None,
    tag_name: targets.TargetName | None = None,
    level: targets.Level | None = None,
    current_parent_id: targets.Identifier | None = None,
    new_name: targets.Name | None = None,
    color: targets.Color | None = None,
    to_level: targets.Level | None = None,
    parent_id: targets.Identifier | None = None,
    parent_name: targets.TargetName | None = None,
) -> object:
    """准备改标签名字、颜色、父级或层级确认，不写入。编号必须同时给当前level(1/2)。
    或用完整原名，同名二级可用current_parent_id区分。仅填要改的字段，颜色空串清空。
    parent_id/parent_name指定一级父标签；to_level=1升为一级。带子标签的一级标签不能降级。
    跨层移动保留文章引用，实际编号可能变化。
    """
    parent = (
        _target(parent_id, parent_name, 1)
        if parent_id is not None or parent_name is not None
        else None
    )
    return _prepare(
        "update",
        _target(tag_id, tag_name, level, current_parent_id),
        {"name": new_name, "color": color, "level": to_level, "parent": parent},
        config,
    )


@tool(return_direct=True)
def prepare_tag_delete(
    config: RunnableConfig,
    tag_id: targets.Identifier | None = None,
    tag_name: targets.TargetName | None = None,
    level: targets.Level | None = None,
    parent_id: targets.Identifier | None = None,
) -> object:
    """准备删除标签确认，不写入。编号需同时提供当前层级；或用完整原名，二级同名可补父编号。
    删除一级会同时删除二级子标签；文章不删除，只清除相应引用。确认卡展示实际影响数量。
    """
    return _prepare("delete", _target(tag_id, tag_name, level, parent_id), {}, config)


def tools(read_tools):
    return {
        **read_tools,
        **{t.name: t for t in (prepare_tag_create, prepare_tag_update, prepare_tag_delete)},
    }
