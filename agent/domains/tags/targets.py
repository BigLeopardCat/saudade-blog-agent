"""Typed dictionary targets and business-owned before/after plans."""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr

from agent.runtime.tool_evidence import observed
from tools import base

Identifier = Annotated[StrictInt, Field(gt=0)]
Name = Annotated[StrictStr, Field(min_length=1, max_length=40)]
TargetName = Annotated[StrictStr, Field(min_length=1, max_length=255)]
Level = Annotated[StrictInt, Field(ge=1, le=2)]
Color = Annotated[StrictStr, Field(pattern=r"^(?:|#[0-9a-fA-F]{6})$")]
PATH = "/api/protected/tag-actions"


class Target(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: Identifier | None = None
    name: TargetName | None = None
    level: Level | None = None
    parentId: Identifier | None = None


class Fields(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: Name | None = None
    color: Color | None = None
    level: Level | None = None
    parent: Target | None = None


class Intent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["create", "update", "delete"]
    target: Target | None = None
    fields: Fields = Field(default_factory=Fields)


class Tag(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: Identifier | None
    level: Level
    name: TargetName
    color: Annotated[StrictStr, Field(max_length=255)] | None
    parentId: Identifier | None


class Plan(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: Annotated[StrictStr, Field(pattern=r"^[0-9a-f]{64}$")]
    before: Tag | None
    after: Tag | None
    parent: Tag | None
    childCount: Annotated[StrictInt, Field(ge=0)]
    articleCount: Annotated[StrictInt, Field(ge=0)]
    revisionCount: Annotated[StrictInt, Field(ge=0)]


def valid_target(t):
    return bool(
        t
        and ((t.id is not None and t.level is not None) or (t.id is None and t.name is not None))
        and not (t.level == 1 and t.parentId is not None)
    )


def intent(value):
    parsed = Intent.model_validate(value)
    t, f = parsed.target, parsed.fields
    if (
        parsed.action == "create"
        and (t is not None or not f.name)
        or parsed.action != "create"
        and not valid_target(t)
        or parsed.action == "delete"
        and f != Fields()
        or parsed.action == "update"
        and f == Fields()
        or f.parent is not None
        and (
            not valid_target(f.parent)
            or f.parent.level not in (None, 1)
            or f.parent.parentId is not None
        )
        or f.parent is not None
        and f.level == 1
        or any(
            s is not None and (not s.strip() or s != s.strip() or any(ord(c) < 32 for c in s))
            for s in (t.name if t else None, f.name, f.parent.name if f.parent else None)
        )
    ):
        raise ValueError("标签以完整原名或层级和编号定位；修改至少一项，一级标签不能设置父级")
    return parsed.model_dump()


def matches(target, row):
    return all(value is None or row[key] == value for key, value in target.items())


def planned_after(expected, before, parent, fields, result, committed):
    after = result["after"]
    expected.update(
        {key: fields[key] for key in ("name", "color", "level") if fields[key] is not None}
    )
    if fields["parent"] is not None:
        if parent is None or not matches(fields["parent"], parent):
            raise ValueError("父标签与请求不匹配")
        expected.update(level=2, parentId=parent["id"])
    if fields["level"] is not None:
        expected["level"] = fields["level"]
    if expected["level"] == 1:
        expected["parentId"] = None
        if parent is not None:
            raise ValueError("一级标签无父级")
    elif (
        parent is None
        or parent["level"] != 1
        or parent["id"] is None
        or parent["parentId"] is not None
        or expected["parentId"] != parent["id"]
    ):
        raise ValueError("二级标签缺少有效一级父标签")
    if before is not None and before["level"] != expected["level"]:
        if before["level"] == 1 and (result["childCount"] or expected["parentId"] == before["id"]):
            raise ValueError("不能降级带子标签的一级标签或挂到自身")
        expected["id"] = None
    if expected["id"] is None and committed:
        if after is None or after["id"] is None:
            raise ValueError("实际结果缺少唯一编号")
        expected["id"] = after["id"]
    return expected


def plan(value, request, *, committed=False):
    result = Plan.model_validate(value).model_dump()
    before, after, parent = result["before"], result["after"], result["parent"]
    action, fields = request["action"], request["fields"]
    if action == "create":
        if before is not None or any(
            result[k] for k in ("childCount", "articleCount", "revisionCount")
        ):
            raise ValueError("新建不能覆盖已有标签或引用")
        expected = {"id": None, "level": 1, "name": "", "color": None, "parentId": None}
    else:
        if before is None or before["id"] is None or not matches(request["target"], before):
            raise ValueError("目标标签与确认请求不一致")
        expected = dict(before)
    if action == "delete":
        expected = None
        if parent is not None:
            raise ValueError("删除不能包含目标父级")
    else:
        expected = planned_after(expected, before, parent, fields, result, committed)
    if after != expected:
        raise ValueError("标签计划包含未请求的字段变更")
    return result


def prepare(request, config):
    result = base._admin_request("POST", PATH + "/prepare", request, config)
    if isinstance(result, base.ToolResult):
        return result
    try:
        return observed(
            "tags",
            plan(result, request),
            ("before", "parent", "childCount", "articleCount", "revisionCount", "version"),
        )
    except (ValueError, TypeError):
        return base.unavailable("标签变更、层级或关联范围无法核对，本次未生成操作。")
