"""Service-owned account operation eligibility; no preparation or execution authority."""

import json
from typing import Annotated, Literal
from urllib.parse import urlencode

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictStr

from agent.authz import SCOPE_ADMIN_CONSOLE
from agent.domains.account_actions.targets import PATH, Action, Identifier
from agent.runtime.access import configured_reads_enabled
from agent.runtime.tool_evidence import observed
from tools import base
from tools.result import invalid_args

RequestedRole = Literal["admin", "user", "zako", "superadmin"]
ReasonCode = Literal[
    "actor_not_permitted",
    "self_target",
    "target_superadmin",
    "peer_admin",
    "target_role_unknown",
    "unknown_role",
    "role_not_assignable",
    "admin_target_tier",
    "admin_assign_tier",
]


class Eligibility(BaseModel):
    model_config = ConfigDict(extra="forbid")
    accountId: Identifier
    action: Action
    role: RequestedRole | None
    allowed: StrictBool
    reasonCode: ReasonCode | None
    message: Annotated[StrictStr, Field(min_length=1, max_length=500)]


def checked(value, account_id, action, role):
    result = Eligibility.model_validate(value).model_dump()
    if (
        result["accountId"] != account_id
        or result["action"] != action
        or result["role"] != role
        or (action == "set_role") != (role is not None)
        or result["allowed"] != (result["reasonCode"] is None)
    ):
        raise ValueError("Account eligibility does not match the requested operation")
    return result


@tool
def get_account_action_eligibility(
    account_id: Identifier,
    action: Action,
    config: RunnableConfig,
    role: RequestedRole | None = None,
) -> object:
    """Check the backend's current permission for an exact account ID and operation.
    Requires no mute duration and never prepares or executes a change. For set_role,
    specify the proposed role; other actions omit role. allowed=true is eligibility,
    not approval. If muting is allowed but no duration is known, ask for the duration
    before preparation. A catalog miss does not establish that an account is absent;
    this endpoint also checks known IDs excluded from account listings.
    """
    if not configured_reads_enabled(config, SCOPE_ADMIN_CONSOLE):
        return base.unavailable("需要已验签且一致的管理员身份。")
    if (action == "set_role") != (role is not None):
        return invalid_args("变更身份必须提供role；其他操作不得提供role。")
    params = {"accountId": account_id, "action": action}
    if role is not None:
        params["role"] = role
    result = base._admin_get(PATH + "/eligibility?" + urlencode(params), config)
    if isinstance(result, base.ToolResult):
        return result
    try:
        value = checked(result, account_id, action, role)
    except (ValueError, TypeError):
        return base.unavailable("账号操作资格无法核对，本次未准备或执行操作。")
    observed(
        "account_action_eligibility",
        value,
        ("accountId", "action", "role", "allowed", "reasonCode", "message"),
    )
    return base.ok(json.dumps(value, ensure_ascii=False, separators=(",", ":")), data=value)
