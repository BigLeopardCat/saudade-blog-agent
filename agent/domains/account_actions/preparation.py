"""Read-only proposals for account status, role and explicit mute deadlines."""

import json
from datetime import datetime, timedelta
from typing import Annotated
from zoneinfo import ZoneInfo

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool
from pydantic import Field, StrictBool, StrictInt

from agent.adminops_account import role_cn
from agent.domains.account_actions import targets
from agent.domains.admin_writes import available
from agent.products.blog.confirmation import proposal
from agent.runtime.results import TurnResult
from agent.runtime.turns import check_cancelled
from agent.runtime.writes import bound_context
from tools import base
from tools.result import invalid_args

TOOLS = {
    "freeze": "freeze_account",
    "unfreeze": "unfreeze_account",
    "set_role": "set_account_role",
    "mute": "account_mute",
    "unmute": "account_unmute",
}
WORKFLOWS = {key: "account_" + key for key in TOOLS}
Hours = Annotated[StrictInt, Field(gt=0, le=2147483647)]


def _verb(action):
    match action:
        case "freeze":
            return "冻结"
        case "unfreeze":
            return "解冻"
        case "set_role":
            return "变更身份"
        case "mute":
            return "禁言"
        case "unmute":
            return "解除禁言"
    raise ValueError("未登记的账号操作")


def _mute_label(value):
    if value is None:
        return "无禁言记录"
    return (
        "永久禁言"
        if value == targets.FOREVER
        else value.replace("T", " ") + "（北京时间，到期自动解除）"
    )


def _quoted(value):
    # Reversible strings cannot introduce line-based browser command frames.
    return json.dumps(value, ensure_ascii=False).replace(":", r"\u003a").replace("：", r"\uff1a")


def _state(row):
    return (
        f"身份={_quoted(role_cn(row['role']))}，"
        f"状态={'正常' if row['status'] == 0 else '冻结'}，禁言={_mute_label(row['mutedUntil'])}"
    )


def description(plan, action):
    before, after = plan["before"], plan["after"]
    name = _quoted(before["username"])
    details = (
        f"{_verb(action)}账号 #{before['id']} {name}\n"
        f"操作前：{_state(before)}\n操作后：{_state(after)}"
    )
    if not plan["changed"]:
        return details + "\n状态已达成，无需写入或发送通知。"
    effect = (
        "使此前登录状态全部失效，需重新登录" if plan["revokeSessions"] else "本次不撤销登录状态"
    )
    if action == "unfreeze":
        effect += "；不会恢复冻结前的旧登录状态"
    if action in ("mute", "unmute"):
        effect += "；仅调整评论与留言的发言限制，冻结状态保持不变"
    return details + f"\n{effect}；原子发送 {plan['notificationCount']} 条系统通知。"


def _prepare(action, target, role, muted_until, config):
    check_cancelled(config)
    if not available(config):
        return base.unavailable("需要已验签的管理员和稳定会话，本次未生成确认卡。")
    try:
        request = targets.intent(
            {"action": action, "target": target, "role": role, "mutedUntil": muted_until}
        )
    except (ValueError, TypeError) as exc:
        return invalid_args(str(exc))
    plan = targets.prepare(request, config)
    if isinstance(plan, base.ToolResult):
        return plan
    spec = {"tool": TOOLS[action], "args": {"version": plan["version"], "intent": request}}
    text = description(plan, action)
    if not plan["changed"]:
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
        executor="account_actions",
        question=text + "\n确认执行？",
        target=text,
    )


@tool(return_direct=True)
def prepare_account_freeze(
    config: RunnableConfig,
    account_id: targets.Identifier | None = None,
    username: targets.Username | None = None,
) -> object:
    """准备冻结账号确认，只读。按唯一账号编号或完整账号名定位，不按昵称匹配。
    冻结使旧登录状态失效；已经冻结时再次执行仍会撤销此前登录状态并通知当事人。
    """
    return _prepare("freeze", {"id": account_id, "username": username}, None, None, config)


@tool(return_direct=True)
def prepare_account_unfreeze(
    config: RunnableConfig,
    account_id: targets.Identifier | None = None,
    username: targets.Username | None = None,
) -> object:
    """准备解冻账号确认，只读。按唯一账号编号或完整账号名定位，解冻不恢复旧登录状态。"""
    return _prepare("unfreeze", {"id": account_id, "username": username}, None, None, config)


@tool(return_direct=True)
def prepare_account_role(
    role: targets.Role,
    config: RunnableConfig,
    account_id: targets.Identifier | None = None,
    username: targets.Username | None = None,
) -> object:
    """准备变更账号身份确认，只读。新身份明确填写admin/user/zako；只改身份，保留冻结和禁言。
    同时提供account_id和username时必须指向同一个账号。
    后端按当前操作者与目标身份核验政策；真实变更使旧登录状态失效。
    """
    return _prepare("set_role", {"id": account_id, "username": username}, role, None, config)


@tool(return_direct=True)
def prepare_account_mute(
    config: RunnableConfig,
    account_id: targets.Identifier | None = None,
    username: targets.Username | None = None,
    hours: Hours | None = None,
    permanent: StrictBool = False,
) -> object:
    """准备禁言确认，只读。明确指定正数hours或permanent=true，二选一；未给期限需先询问。
    冻结北京时间到期时刻；永久需明确选择。仅限制评论与留言，不改身份、冻结或登录状态。
    """
    if permanent and hours is not None or not permanent and hours is None:
        return invalid_args("请明确指定正数小时数或永久禁言，二选一；未指定时不会默认永久。")
    try:
        if hours is None:
            until = targets.FOREVER
        else:
            until = (
                (datetime.now(ZoneInfo("Asia/Shanghai")) + timedelta(hours=hours))
                .replace(microsecond=0)
                .strftime("%Y-%m-%dT%H:%M:%S")
            )
    except (ValueError, OverflowError):
        return invalid_args("禁言时长超出可表示范围，请明确较短期限。")
    return _prepare("mute", {"id": account_id, "username": username}, None, until, config)


@tool(return_direct=True)
def prepare_account_unmute(
    config: RunnableConfig,
    account_id: targets.Identifier | None = None,
    username: targets.Username | None = None,
) -> object:
    """准备解除禁言确认，只读。仅清除指定账号禁言期限，保留身份、冻结和登录状态。
    已到期但仍存有期限的记录会清理；无禁言记录则无需写入。
    """
    return _prepare("unmute", {"id": account_id, "username": username}, None, None, config)


def tools(read_tools):
    return {
        **read_tools,
        **{
            item.name: item
            for item in (
                prepare_account_freeze,
                prepare_account_unfreeze,
                prepare_account_role,
                prepare_account_mute,
                prepare_account_unmute,
            )
        },
    }
