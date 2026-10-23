"""Exact recipients, bounded original text and service-owned notice plans."""

from typing import Annotated
from unicodedata import category

from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr

from agent.principal import KNOWN_ROLES, ROLE_SUPERADMIN
from tools import base

Identifier = Annotated[StrictInt, Field(gt=0)]
Username = Annotated[StrictStr, Field(min_length=1, max_length=255)]
Title = Annotated[StrictStr, Field(max_length=128)]
Content = Annotated[StrictStr, Field(min_length=1, max_length=1000)]
Version = Annotated[StrictStr, Field(pattern=r"^[0-9a-f]{64}$")]
PATH = "/api/protected/notice-actions"
DEFAULT_TITLE = "站内通知"


class Target(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: Identifier | None = None
    username: Username | None = None


class Intent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    target: Target
    title: Title | None = None
    content: Content


class Recipient(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: Identifier
    username: Username
    role: StrictStr


class Plan(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: Version
    recipient: Recipient
    title: Title
    content: Content


def intent(value):
    parsed = Intent.model_validate(value)
    target = parsed.target
    title = parsed.title
    if (
        (target.id is None) == (target.username is None)
        or target.username is not None
        and target.username != target.username.strip()
        or not parsed.content.strip()
        or any(category(c) == "Cc" and c not in "\n\r\t" for c in parsed.content)
        or any(category(c) == "Cc" for c in (title or "") + (target.username or ""))
    ):
        raise ValueError("需唯一账号编号或完整账号名，以及非空正文；正文保留原文")
    return parsed.model_dump()


def plan(value, request):
    result = Plan.model_validate(value).model_dump()
    target, recipient = request["target"], result["recipient"]
    expected_title = request["title"]
    if expected_title is None or not expected_title.strip():
        expected_title = DEFAULT_TITLE
    if (
        recipient["role"] not in KNOWN_ROLES
        or recipient["role"] == ROLE_SUPERADMIN
        or any(v is not None and recipient[k] != v for k, v in target.items())
        or result["title"] != expected_title
        or result["content"] != request["content"]
    ):
        raise ValueError("收件人或通知全文与请求不一致")
    return result


def prepare(request, config):
    result = base._admin_request("POST", PATH + "/prepare", request, config)
    if isinstance(result, base.ToolResult):
        return result
    try:
        return plan(result, request)
    except (TypeError, ValueError):
        return base.unavailable("收件人或通知原文无法核对，本次未生成确认。")
