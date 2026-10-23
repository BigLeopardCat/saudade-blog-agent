"""Bounded category requests and service-owned confirmation snapshots."""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr

from tools import base

Identifier = Annotated[StrictInt, Field(gt=0)]
Name = Annotated[StrictStr, Field(min_length=1, max_length=40)]
Text = Annotated[StrictStr, Field(max_length=255)]
TargetName = Annotated[StrictStr, Field(min_length=1, max_length=255)]
Color = Annotated[StrictStr, Field(pattern=r"^(?:|#[0-9a-fA-F]{6})$")]
PATH = "/api/protected/category-actions"


class Target(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: Identifier | None = None
    name: TargetName | None = None


class Fields(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: Name | None = None
    pathName: Text | None = None
    introduce: Text | None = None
    icon: Text | None = None
    color: Color | None = None


class Intent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["create", "update", "delete"]
    target: Target | None = None
    fields: Fields = Field(default_factory=Fields)


class Category(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: Identifier | None
    name: TargetName
    pathName: Text | None
    introduce: Text | None
    icon: Text | None
    color: Annotated[StrictStr, Field(max_length=50)] | None


class Plan(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: Annotated[StrictStr, Field(pattern=r"^[0-9a-f]{64}$")]
    before: Category | None
    after: Category | None
    articleCount: Annotated[StrictInt, Field(ge=0)]
    revisionCount: Annotated[StrictInt, Field(ge=0)]


def intent(value):
    parsed = Intent.model_validate(value)
    target, fields = parsed.target, parsed.fields.model_dump(exclude_none=True)
    valid_target = target and ((target.id is None) != (target.name is None))
    if (
        parsed.action == "create"
        and (target is not None or not fields.get("name"))
        or parsed.action != "create"
        and not valid_target
        or parsed.action == "delete"
        and fields
        or parsed.action == "update"
        and not fields
        or any(
            value is not None and (not value.strip() or value != value.strip())
            for value in (parsed.fields.name, target.name if target else None)
        )
    ):
        raise ValueError("创建需名字，修改或删除需唯一编号或完整原名；修改至少指定一个字段")
    return parsed.model_dump()


def plan(value, request, *, committed=False):
    result = Plan.model_validate(value).model_dump()
    before, after = result["before"], result["after"]
    action = request["action"]
    if action == "create":
        if before is not None or result["articleCount"] or result["revisionCount"]:
            raise ValueError("创建计划不能覆盖已有分类")
        expected = dict.fromkeys(("id", "name", "pathName", "introduce", "icon", "color"))
        if committed:
            if after is None or after["id"] is None:
                raise ValueError("创建回执缺少唯一编号")
            expected["id"] = after["id"]
    else:
        if before is None or before["id"] is None:
            raise ValueError("分类目标缺少唯一编号")
        target = request["target"]
        if (target["id"] is not None and target["id"] != before["id"]) or (
            target["name"] is not None and target["name"] != before["name"]
        ):
            raise ValueError("分类目标不匹配")
        expected = dict(before)
    if action == "delete":
        expected = None
    else:
        expected.update(
            {key: value for key, value in request["fields"].items() if value is not None}
        )
        if action == "create" and expected["pathName"] is None:
            expected["pathName"] = expected["name"]
    if after != expected:
        raise ValueError("服务计划包含未请求的字段变更")
    return result


def prepare(request, config):
    result = base._admin_request("POST", PATH + "/prepare", request, config)
    if isinstance(result, base.ToolResult):
        return result
    try:
        return plan(result, request)
    except (ValueError, TypeError):
        return base.unavailable("分类变更与影响范围无法核对，本次未生成操作。")
