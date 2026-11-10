"""Read category state and prepare an explicit create, update or delete proposal."""

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool

from agent.domains.admin_writes import available
from agent.domains.categories import targets
from agent.proposals import proposal
from agent.reports import sanitize_untrusted
from agent.runtime.results import TurnResult
from agent.runtime.turns import check_cancelled
from agent.runtime.writes import bound_context
from agent.scheduling.models import WriteSchedule
from tools import base
from tools.result import invalid_args

TOOLS = {"create": "create_category", "update": "update_category", "delete": "delete_category"}
WORKFLOWS = {"create": "category_create", "update": "category_update", "delete": "category_delete"}
LABELS = {"name": "名字", "pathName": "路径", "introduce": "简介", "icon": "图标", "color": "颜色"}


def _value(value):
    if value is None:
        return "（未设置）"
    return sanitize_untrusted(value, 255) if value else "（空）"


def description(plan):
    before, after = plan["before"], plan["after"]
    target = before or after
    heading = f"分类 #{target['id']}「{_value(target['name'])}」" if target["id"] else "新分类"
    if after is None:
        details = "删除分类，文章不删除，关联记录改为无分类"
    else:
        fields = [
            f"{label}：" + (f"{_value(before[key])} → " if before else "") + _value(after[key])
            for key, label in LABELS.items()
            if before is None or before[key] != after[key]
        ]
        details = "；".join(fields) or "字段已达成，无需改动"
    scope = (
        f"关联 {plan['articleCount']} 篇文章（含私密文章及独立草稿）"
        f"、{plan['revisionCount']} 份编辑草稿"
    )
    return f"{heading}：{details}。{scope}"


def _prepare(action, target, fields, config, schedule=None):
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
    args = {"version": plan["version"], "intent": request}
    spec = {"tool": TOOLS[action], "args": args}
    text = description(plan)
    if schedule is None and plan["before"] == plan["after"]:
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
        executor="categories",
        question=text + "。确认执行？",
        target=text,
        schedule=schedule,
    )


@tool(return_direct=True)
def prepare_category_create(
    name: targets.Name,
    config: RunnableConfig,
    path_name: targets.Text | None = None,
    introduce: targets.Text | None = None,
    icon: targets.Text | None = None,
    color: targets.Color | None = None,
    schedule: WriteSchedule | None = None,
) -> object:
    """Optional schedule delays this exact approved change; use run_at for clock times.
    准备新建文章分类确认，不写入。默认以名字作路径；含空格或路径保留字符时需另填path_name。
    可选简介、图标和#RRGGBB颜色，不填保持未设置；名字和非空路径不能与已有分类冲突。
    影响数量分为全部关联文章（含私密、独立草稿）与编辑草稿。
    """
    return _prepare(
        "create",
        None,
        {"name": name, "pathName": path_name, "introduce": introduce, "icon": icon, "color": color},
        config,
        schedule,
    )


@tool(return_direct=True)
def prepare_category_update(
    config: RunnableConfig,
    category_id: targets.Identifier | None = None,
    category_name: targets.TargetName | None = None,
    new_name: targets.Name | None = None,
    path_name: targets.Text | None = None,
    introduce: targets.Text | None = None,
    icon: targets.Text | None = None,
    color: targets.Color | None = None,
    schedule: WriteSchedule | None = None,
) -> object:
    """Optional schedule delays this exact approved change; use run_at for clock times.
    准备修改分类确认，不写入。按category_id或完整category_name定位，同时提供时交叉核对。
    仅填写要改的字段；new_name是新名字。简介、图标和颜色可传空串清空；路径不能清空。
    名字/路径的变化影响所有关联文章的分类展示；数量分为全部文章（含私密、独立草稿）与编辑草稿。
    """
    return _prepare(
        "update",
        {"id": category_id, "name": category_name},
        {
            "name": new_name,
            "pathName": path_name,
            "introduce": introduce,
            "icon": icon,
            "color": color,
        },
        config,
        schedule,
    )


@tool(return_direct=True)
def prepare_category_delete(
    config: RunnableConfig,
    category_id: targets.Identifier | None = None,
    category_name: targets.TargetName | None = None,
    schedule: WriteSchedule | None = None,
) -> object:
    """Optional schedule delays this exact approved change; use run_at for clock times.
    准备删除分类确认，不写入。以唯一编号或完整原名定位；文章不删除，关联记录改为无分类。
    确认卡分别计数全部关联文章（含私密、独立草稿）与编辑草稿；关联范围变化后必须重新确认。
    """
    return _prepare("delete", {"id": category_id, "name": category_name}, {}, config, schedule)


def tools(read_tools):
    return {
        **read_tools,
        **{
            item.name: item
            for item in (prepare_category_create, prepare_category_update, prepare_category_delete)
        },
    }
