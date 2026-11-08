"""Validate authoritative library snapshots without interpreting image contents."""

from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    StrictStr,
)

from agent.runtime.tool_evidence import observed
from tools import base

PATH = "/api/protect/gallery"
Identifier = Annotated[StrictInt, Field(gt=0)]
Digest = Annotated[StrictStr, Field(pattern=r"^[0-9a-f]{64}$")]


class Image(BaseModel):
    imageKey: Identifier
    imageUrl: Annotated[StrictStr, Field(min_length=1, max_length=2048)]
    displayName: Annotated[StrictStr, Field(max_length=255)] | None = None
    description: Annotated[StrictStr, Field(max_length=2000)]
    folderId: Annotated[StrictInt, Field(ge=0)]
    folderName: Annotated[StrictStr, Field(max_length=80)] | None = None
    folderParentId: Annotated[StrictInt, Field(ge=0)] | None = None
    isFavorite: StrictBool
    version: Digest


class Cover(BaseModel):
    articleId: Identifier
    title: Annotated[StrictStr, Field(max_length=255)]
    coverUrl: str | None = None
    fit: Literal["cover", "contain"] | None = None
    version: Digest


class CoverArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")
    article_id: Identifier
    version: Digest
    image_id: Identifier
    image_version: Digest
    image_url: Annotated[StrictStr, Field(min_length=1, max_length=2048)]
    fit: Literal["cover", "contain"]


def image(image_id, config):
    value = base._admin_get(f"{PATH}/images/{image_id}", config)
    if isinstance(value, base.ToolResult):
        return value
    try:
        row = Image.model_validate(value).model_dump()
        if row["imageKey"] != image_id:
            raise ValueError("Image identity mismatch")
        return observed("gallery_image", row, tuple(row))
    except (ValueError, TypeError):
        return base.unavailable("未能核对图片信息，本次未进行操作。")


def cover(article_id, config):
    value = base._admin_get(f"{PATH}/articles/{article_id}/cover", config)
    if isinstance(value, base.ToolResult):
        return value
    try:
        row = Cover.model_validate(value).model_dump()
        if row["articleId"] != article_id:
            raise ValueError("Article identity mismatch")
        return observed("gallery_cover", row, tuple(row))
    except (ValueError, TypeError):
        return base.unavailable("未能核对文章及封面版本，本次未进行操作。")
