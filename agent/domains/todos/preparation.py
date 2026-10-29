"""Prepare one concrete todo change without issuing a business write."""

from typing import Annotated, Literal

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool
from pydantic import Field, StrictInt, StrictStr

from agent.domains.todos import targets
from agent.products.blog.confirmation import proposal
from agent.runtime.results import TurnResult
from agent.runtime.turns import check_cancelled
from agent.runtime.writes import bound_context
from tools import base

available = targets.available
_ACTIONS = {
    "create": ("dashboard_todo_add", "create_dashboard_todo"),
    "complete": ("dashboard_todo_done", "complete_dashboard_todo"),
    "reschedule": ("dashboard_todo_reschedule", "reschedule_dashboard_todo"),
}


def _arguments(action, text, item_id, date, rows):
    if action == "create":
        if item_id is not None or len(rows) >= targets.MAX_ROWS:
            raise ValueError("新增待办不可指定已有编号，且列表不可超过 200 条")
        return {"text": targets.text(text), "date": targets.due_date(date)}, None
    if text is not None or item_id not in rows:
        raise ValueError("请先读取列表，再按已有待办编号选择；不能按正文猜测目标")
    current = rows[item_id]
    args = {"item_id": item_id, "version": current["version"], "text": current["text"]}
    if action == "complete":
        if date is not None:
            raise ValueError("标记完成不接受排期参数")
    else:
        args["date"] = targets.due_date(date)
    return args, current


def _prepare(action, text, item_id, date, config):
    check_cancelled(config)
    if not available(config):
        return base.unavailable("需要已验签的管理员身份和稳定会话，本次未生成确认卡。")
    if date == "":
        if action == "reschedule":
            return base.unavailable("请明确新排期日；清空排期须显式传 null，本次未生成确认卡。")
        date = None
    rows, error = targets.snapshot(config)
    if error is not None:
        return error
    try:
        args, current = _arguments(action, text, item_id, date, rows)
    except (ValueError, TypeError) as exc:
        return base.unavailable(f"{exc}，本次未生成确认卡。")
    workflow, name = _ACTIONS[action]
    spec = {"tool": name, "args": args}
    satisfied = current is not None and (
        current["done"] if action == "complete" else current["date"] == args["date"]
    )
    if satisfied:
        reply = "该待办已处于所请求的状态，本次无需改动。"
        return TurnResult(
            reply,
            {"noop_note": reply, "noop_text": reply, "receipts": [], "task_satisfied": [spec]},
        )
    label = {"create": "新增待办", "complete": "将待办标记完成", "reschedule": "修改待办排期"}[
        action
    ]
    target = f"{label}「{args['text']}」"
    if item_id is not None:
        target += f"（编号 {item_id}）"
    if action != "complete":
        target += f"；排期：{args['date'] or '未排期'}"
    principal, cid = bound_context(config)
    check_cancelled(config)
    return proposal(
        principal,
        cid,
        workflow,
        spec,
        executor="todos",
        target=target,
        question=target + "？",
    )


@tool(return_direct=True)
def prepare_dashboard_todo(
    action: Literal["create", "complete", "reschedule"],
    config: RunnableConfig,
    text: Annotated[StrictStr, Field(min_length=1, max_length=200)] | None = None,
    item_id: Annotated[StrictInt, Field(gt=0)] | None = None,
    date: Annotated[StrictStr | None, Field(description=(
        "Due date explicitly requested by the user (YYYY-MM-DD); omit for an unscheduled "
        "create, null clears a reschedule."))] = "",
) -> object:
    """按用户明确要求准备一项后台个人待办操作，不执行写入。

    create 提供原文 text；complete/reschedule 先读列表并提供 item_id，不传 text。
    排期只接受 YYYY-MM-DD。create 未指定排期时省略 date，不从聊天时间推断。
    reschedule 必须提供 date；清空排期显式传 null，省略或空串不清空。
    """
    return _prepare(action, text, item_id, date, config)


def tools(read_tools):
    return {
        **{name: tool for name, tool in read_tools.items() if name == "list_dashboard_todos"},
        prepare_dashboard_todo.name: prepare_dashboard_todo,
    }
