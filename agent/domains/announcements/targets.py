"""Exact announcement requests and service-owned frozen plans."""

from typing import Annotated, Literal
from unicodedata import category

from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr

from agent.runtime.tool_evidence import observed
from tools import base

Identifier = Annotated[StrictInt, Field(gt=0)]
Title = Annotated[StrictStr, Field(min_length=1, max_length=80)]
Content = Annotated[StrictStr, Field(min_length=1, max_length=2000)]
PATH = "/api/protected/announcement-actions"


class Target(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: Identifier | None = None
    title: Title | None = None


class Fields(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: Title | None = None
    content: Content | None = None


class Intent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["create", "update", "delete"]
    target: Target | None = None
    fields: Fields = Field(default_factory=Fields)


class Announcement(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: Identifier | None
    title: StrictStr
    content: StrictStr


class Plan(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: Annotated[StrictStr, Field(pattern=r"^[0-9a-f]{64}$")]
    before: Announcement | None
    after: Announcement | None
    notificationCount: Annotated[StrictInt, Field(ge=0)]


def intent(value):
    parsed = Intent.model_validate(value)
    target, fields = parsed.target, parsed.fields.model_dump(exclude_none=True)
    valid_target = target and (target.id is not None or target.title is not None)
    if (
        parsed.action == "create"
        and (target is not None or set(fields) != {"title", "content"})
        or parsed.action != "create"
        and not valid_target
        or parsed.action == "delete"
        and fields
        or parsed.action == "update"
        and not fields
        or any(
            value is not None and not value.strip() for value in parsed.fields.model_dump().values()
        )
        or parsed.fields.title is not None
        and any(category(char) == "Cc" for char in parsed.fields.title)
        or parsed.fields.content is not None
        and any(category(char) == "Cc" and char not in "\n\r\t" for char in parsed.fields.content)
        or target is not None
        and target.title is not None
        and not target.title.strip()
    ):
        raise ValueError("创建需标题和正文；修改或删除需唯一编号或完整原标题，修改至少指定一项")
    return parsed.model_dump()


def plan(value, request, *, committed=False):
    result = Plan.model_validate(value).model_dump()
    before, after = result["before"], result["after"]
    if request["action"] == "create":
        if before is not None:
            raise ValueError("创建计划不能覆盖已有公告")
        expected = {"id": None, **request["fields"]}
        if committed:
            if after is None or after["id"] is None:
                raise ValueError("公告创建回执缺少唯一编号")
            expected["id"] = after["id"]
    else:
        if before is None or before["id"] is None:
            raise ValueError("公告目标缺少唯一编号")
        target = request["target"]
        if (target["id"] is not None and target["id"] != before["id"]) or (
            target["title"] is not None and target["title"] != before["title"]
        ):
            raise ValueError("公告目标不匹配")
        expected = dict(before)
        expected.update(
            {key: value for key, value in request["fields"].items() if value is not None}
        )
    if request["action"] == "delete":
        expected = None
    if after != expected:
        raise ValueError("公告计划包含未请求的变更")
    return result


def prepare(request, config):
    result = base._admin_request("POST", PATH + "/prepare", request, config)
    if isinstance(result, base.ToolResult):
        return result
    try:
        return observed(
            "announcements", plan(result, request), ("before", "notificationCount", "version")
        )
    except (ValueError, TypeError):
        return base.unavailable("公告变更与通知范围无法核对，本次未生成操作。")
