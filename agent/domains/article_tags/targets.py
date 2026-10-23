"""Validate bounded tag intents and versioned service plans."""

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr

from tools import base

Identifier = Annotated[StrictInt, Field(gt=0)]
Name = Annotated[StrictStr, Field(min_length=1, max_length=40)]
Names = Annotated[list[Name], Field(max_length=10)]
Level = Annotated[StrictInt, Field(ge=1, le=2)]
PATH = "/api/protected/article-tag-actions/"


class Intent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    add: Names = Field(default_factory=list)
    remove: Names = Field(default_factory=list)
    replace: Names | None = None
    level: Level | None = None
    parent_tag: Name | None = None
    color: Annotated[StrictStr, Field(pattern=r"^#[0-9a-fA-F]{6}$")] | None = None


class Tag(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: Identifier | None
    level: Level
    name: Name
    parentId: Identifier | None
    color: StrictStr | None


class Plan(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: Identifier
    title: Annotated[StrictStr, Field(max_length=255)]
    version: Annotated[StrictStr, Field(pattern=r"^[0-9a-f]{64}$")]
    before: Annotated[list[Tag], Field(max_length=40)]
    after: Annotated[list[Tag], Field(max_length=40)]


def intent(value):
    parsed = Intent.model_validate(value)
    groups = (parsed.add, parsed.remove, parsed.replace or [])
    if (
        sum(len(group) for group in groups) > 10
        or any(
            len(set(group)) != len(group) or any(not name.strip() for name in group)
            for group in groups
        )
        or set(parsed.add) & set(parsed.remove)
        or (parsed.replace is not None and (parsed.add or parsed.remove))
        or (parsed.replace is None and not parsed.add and not parsed.remove)
        or (parsed.level == 1 and parsed.parent_tag is not None)
        or (parsed.parent_tag is not None and not parsed.parent_tag.strip())
    ):
        raise ValueError("标签名或增删替换参数无效")
    return parsed.model_dump()


def plan(value, article_id):
    parsed = Plan.model_validate(value)
    if parsed.id != article_id:
        raise ValueError("文章目标不一致")
    for group, existing in ((parsed.before, True), (parsed.after, False)):
        seen = set()
        for tag in group:
            key = (tag.level, tag.id, tag.name if tag.id is None else None)
            if (
                key in seen
                or (existing and tag.id is None)
                or (tag.level == 1 and tag.parentId is not None)
                or (tag.level == 2 and tag.parentId is None)
            ):
                raise ValueError("标签身份或层级无效")
            seen.add(key)
    return parsed.model_dump()


def prepare(article_id, request, config):
    result = base._admin_request("POST", PATH + str(article_id) + "/prepare", request, config)
    if isinstance(result, base.ToolResult):
        return result
    try:
        return plan(result, article_id)
    except (ValueError, TypeError):
        return base.unavailable("未能核对文章与标签变更，本次未生成或执行操作。")


def matches(actual, planned):
    if len(actual) != len(planned):
        return False
    return all(
        row["id"] is not None
        and all(
            row[key] == value for key, value in wanted.items() if key != "id" or value is not None
        )
        for row, wanted in zip(actual, planned)
    )
