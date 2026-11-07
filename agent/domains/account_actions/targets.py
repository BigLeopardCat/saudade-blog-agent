"""Exact account intents and strictly checked, non-sensitive confirmation plans."""

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr

from agent.principal import ROLE_ADMIN, ROLE_USER, ROLE_ZAKO
from agent.runtime.tool_evidence import observed
from tools import base

Identifier = Annotated[StrictInt, Field(gt=0, le=2147483647)]
Username = Annotated[StrictStr, Field(min_length=1, max_length=255)]


class Role(StrEnum):
    ADMIN = ROLE_ADMIN
    USER = ROLE_USER
    ZAKO = ROLE_ZAKO


Deadline = Annotated[StrictStr, Field(pattern=r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}$")]
FOREVER = "9999-12-31T23:59:59"
PATH = "/api/protected/account-actions"
Action = Literal["freeze", "unfreeze", "set_role", "mute", "unmute"]


class Target(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: Identifier | None = None
    username: Username | None = None


class Intent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Action
    target: Target
    role: Role | None = None
    mutedUntil: Deadline | None = None


class PrepareRequest(Intent):
    hours: Annotated[StrictInt, Field(gt=0, le=2147483647)] | None = None
    permanent: StrictBool = False


class Account(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: Identifier
    username: Username
    role: StrictStr
    status: StrictInt
    mutedUntil: Deadline | None


class Plan(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: Annotated[StrictStr, Field(pattern=r"^[0-9a-f]{64}$")]
    before: Account
    after: Account
    changed: StrictBool
    revokeSessions: StrictBool
    notificationCount: Annotated[StrictInt, Field(ge=0, le=1)]


def _username(value):
    return (
        value.strip() == value
        and bool(value.strip())
        and not any(ord(char) < 32 or ord(char) == 127 for char in value)
    )


def _identity(parsed):
    target = parsed.target
    if target.id is None and target.username is None:
        raise ValueError("缺少目标账号：请填写account_id或完整username；同时填写时必须指向同一账号")
    if target.username is not None and not _username(target.username):
        raise ValueError("username必须是完整账号名，不能包含首尾空格或控制字符")
    if (parsed.action == "set_role") != (parsed.role is not None):
        raise ValueError("只有变更身份操作必须填写role，合法值为admin/user/zako")


def intent(value):
    parsed = Intent.model_validate(value)
    _identity(parsed)
    if (parsed.action == "mute") != (parsed.mutedUntil is not None):
        raise ValueError("只有禁言操作必须填写到期时间mutedUntil或永久期限")
    if parsed.mutedUntil is not None:
        datetime.fromisoformat(parsed.mutedUntil)
    return parsed.model_dump(mode="json")


def preparation_request(value):
    parsed = PrepareRequest.model_validate(value)
    _identity(parsed)
    choices = sum((parsed.hours is not None, parsed.permanent, parsed.mutedUntil is not None))
    if choices != (1 if parsed.action == "mute" else 0):
        raise ValueError("禁言须明确指定小时数、永久或绝对到期时间，且只能选择一项")
    return parsed.model_dump(mode="json")


def plan(value, request):
    result = Plan.model_validate(value).model_dump()
    before, after = result["before"], result["after"]
    target, action = request["target"], request["action"]
    if (
        target["id"] is not None
        and target["id"] != before["id"]
        or target["username"] is not None
        and target["username"] != before["username"]
        or not _username(before["username"])
    ):
        raise ValueError("账号身份与请求不符")
    for row in (before, after):
        if row["mutedUntil"] is not None:
            datetime.fromisoformat(row["mutedUntil"])
    expected = dict(before)
    if action in ("freeze", "unfreeze"):
        expected["status"] = 1 if action == "freeze" else 0
    elif action == "set_role":
        expected["role"] = request["role"]
    else:
        expected["mutedUntil"] = request["mutedUntil"] if action == "mute" else None
    # Freezing again deliberately revokes sessions, even when the visible status is unchanged.
    changed = action == "freeze" or before != expected
    revoke = action == "freeze" or action == "set_role" and changed
    if (
        after != expected
        or result["changed"] != changed
        or result["revokeSessions"] != revoke
        or result["notificationCount"] != int(changed)
    ):
        raise ValueError("账号计划包含未请求的变更或不一致的副作用")
    return result


def prepare(request, config):
    result = base._admin_read_post(PATH + "/prepare", request, config)
    if isinstance(result, base.ToolResult):
        return result
    try:
        if not isinstance(result, dict) or set(result) != {"plan", "intent"}:
            raise ValueError("Invalid preparation response")
        normalized = intent(result["intent"])
        if (any(normalized[key] != request[key] for key in ("action", "target", "role"))
                or request["mutedUntil"] is not None
                and normalized["mutedUntil"] != request["mutedUntil"]
                or request["permanent"] and normalized["mutedUntil"] != FOREVER):
            raise ValueError("Normalized intent does not match the requested action")
        return observed(
            "account_actions",
            {**plan(result["plan"], normalized), "intent": normalized},
            ("before", "notificationCount", "revokeSessions", "version"),
        )
    except (ValueError, TypeError):
        return base.unavailable("账号状态或实际副作用无法核对，本次未生成操作。")
