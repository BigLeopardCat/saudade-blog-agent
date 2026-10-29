"""Prepare one signed quota action against a concrete request or account."""

from typing import Annotated, Literal

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool
from pydantic import Field, StrictInt, StrictStr

from agent import action_text
from agent.domains.quota_actions import targets
from agent.products.blog.confirmation import proposal
from agent.reports import sanitize_untrusted
from agent.runtime.results import TurnResult
from agent.runtime.turns import check_cancelled
from agent.runtime.writes import bound_context
from tools import base

available = targets.available
ACTIONS = {"approve": ("quota_approve", "approve_quota_request"),
           "reject": ("quota_reject", "reject_quota_request"),
           "reset": ("quota_reset", "reset_user_quota")}


def _prepare(action, request_id, account_id, reason, config):
    check_cancelled(config)
    if not available(config):
        return base.unavailable("需要已验签的管理员身份和稳定会话，本次未生成确认卡。")
    if ((action == "reset" and (account_id is None or request_id is not None))
            or (action != "reset" and (request_id is None or account_id is not None))
            or (action != "reject" and reason is not None)):
        return base.unavailable("审批须指定申请编号，主动重置须指定账号编号；不得混用目标。")
    try:
        if action == "reject":
            targets.reason(reason)
    except ValueError as exc:
        return base.unavailable(str(exc))
    current, error = targets.read(config, request_id=request_id, user_id=account_id)
    if error is not None:
        return error
    args = {"user_id": current["userId"], "name": current["name"],
            "version": current["version"]}
    workflow, name = ACTIONS[action]
    if action == "reset":
        if current["unlimited"] or current["used"] == 0:
            reply = ("该账号不限额，无需重置。" if current["unlimited"]
                     else "该账号已用轮数为零，无需重置。")
            return TurnResult(reply, {"receipts": [], "noop_note": reply, "noop_text": reply})
    else:
        if current["request"]["status"] != 0:
            return base.unavailable("这份申请已经处理，不再生成审批确认卡。")
        args["request_id"] = request_id
        if action == "reject":
            args["reason"] = reason
    target = action_text.receipt_action(name, args, {"account_name": current["name"]})
    target += f"（账号编号{current['userId']}）"
    if action != "reset":
        request = current["request"]
        target += f"；申请编号{request_id}；申请理由："
        target += sanitize_untrusted(request["reason"] or "未填写", 500)
    if action == "reject":
        target += f"；发送给申请人的驳回理由：{sanitize_untrusted(reason, 255)}；不重置额度"
    else:
        target += ("；当前账号不限额" if current["unlimited"] else
                   f"；额度按对话轮计，已用{current['used']}轮，重置后可用{current['limit']}轮")
    principal, cid = bound_context(config)
    check_cancelled(config)
    return proposal(principal, cid, workflow, {"tool": name, "args": args},
                    executor="quota_actions", target=target, question=target + "？")


@tool(return_direct=True)
def prepare_quota_action(
    action: Annotated[Literal["approve", "reject", "reset"], Field(description=
        "approve approves a pending application and restores quota; reject declines a pending "
        "application; reset directly resets an account without an application or reason.")],
    config: RunnableConfig,
    request_id: Annotated[StrictInt, Field(gt=0, description=
        "Required only for approve/reject: pending quota application ID, not an account ID. "
        "Omit for reset.")] | None = None,
    account_id: Annotated[StrictInt, Field(gt=0, description=
        "Required only for reset: account ID, not an application ID. Omit for approve/reject.")]
        | None = None,
    reason: Annotated[StrictStr, Field(min_length=1, max_length=255, description=
        "Required only for reject: the user's explicit rejection reason (1-255 characters), "
        "sent to the applicant. Omit for approve/reset.")] | None = None,
) -> object:
    """准备单项额度确认，不执行写入。

    reset按账号独立重置，无需申请或理由，也不修改历史申请。
    额度单位为对话轮；不限额或已用轮数为零时无需重置。
    """
    return _prepare(action, request_id, account_id, reason, config)


def tools(read_tools):
    return {**{name: tool for name, tool in read_tools.items() if name == "list_quota_requests"},
            prepare_quota_action.name: prepare_quota_action}
