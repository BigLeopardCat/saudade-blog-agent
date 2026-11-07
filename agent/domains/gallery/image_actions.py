"""Bounded gallery intents and authoritative prepare/commit facts."""

import json
import time
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    StrictStr,
    TypeAdapter,
    field_validator,
    model_validator,
)
from pydantic.json_schema import SkipJsonSchema

from agent.domains.gallery.targets import Digest, Identifier

PATH = "/api/protect/gallery/image-actions"
IDS = Annotated[list[Identifier], Field(
    min_length=1, max_length=100, json_schema_extra={"uniqueItems": True})]


class Patch(BaseModel):
    model_config = ConfigDict(extra="forbid", json_schema_extra={"minProperties": 1})
    displayName: Annotated[StrictStr, Field(min_length=1, max_length=255)] | SkipJsonSchema[
        None] = Field(default_factory=lambda: None)
    description: Annotated[StrictStr, Field(max_length=2000)] | SkipJsonSchema[
        None] = Field(default_factory=lambda: None)
    folderId: Annotated[StrictInt, Field(ge=0)] | SkipJsonSchema[
        None] = Field(default_factory=lambda: None)
    isFavorite: StrictBool | SkipJsonSchema[None] = Field(default_factory=lambda: None)

    @field_validator("*", mode="before")
    @classmethod
    def reject_null(cls, value):
        if value is None:
            raise ValueError("omit unchanged fields; explicit null is not a gallery change")
        return value

    @field_validator("displayName")
    @classmethod
    def trim_name(cls, value):
        if not value.strip():
            raise ValueError("a display name cannot be blank")
        return value.strip()

    @model_validator(mode="after")
    def nonempty(self):
        if not self.model_dump(exclude_none=True):
            raise ValueError("specify at least one image metadata change")
        return self


class Item(BaseModel):
    model_config = ConfigDict(extra="forbid")
    imageId: Identifier
    changes: Patch


def _bounded(value, ids):
    if len(ids) != len(set(ids)):
        raise ValueError("image IDs must be unique")
    encoded = json.dumps(value.model_dump(exclude_none=True), ensure_ascii=False,
                         separators=(",", ":")).encode()
    if len(encoded) > 8192:
        raise ValueError("gallery intent exceeds 8192 UTF-8 bytes; use a smaller selection")
    return value


class Selection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    imageIds: IDS

    @model_validator(mode="after")
    def normalize(self):
        self.imageIds = sorted(self.imageIds)
        return _bounded(self, self.imageIds)

    def identifiers(self):
        return self.imageIds


class Move(Selection):
    action: Literal["move"]
    folderId: Annotated[StrictInt, Field(ge=0)]


class Favorite(Selection):
    action: Literal["favorite"]
    isFavorite: StrictBool


class Delete(Selection):
    action: Literal["delete"]


class Metadata(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["metadata"]
    items: Annotated[list[Item], Field(min_length=1, max_length=100)]

    @model_validator(mode="after")
    def normalize(self):
        self.items = sorted(self.items, key=lambda item: item.imageId)
        return _bounded(self, self.identifiers())

    def identifiers(self):
        return [item.imageId for item in self.items]


Intent = Annotated[Move | Favorite | Metadata | Delete, Field(discriminator="action")]
_INTENT: TypeAdapter[Move | Favorite | Metadata | Delete] = TypeAdapter(Intent)


def parse(value):
    return _INTENT.validate_python(value)


class Preview(BaseModel):
    model_config = ConfigDict(extra="forbid")
    imageId: Identifier
    displayName: Annotated[StrictStr, Field(max_length=255)] | None
    folderId: Annotated[StrictInt, Field(ge=0)]
    isFavorite: StrictBool


class Destination(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: Annotated[StrictInt, Field(ge=0)]
    name: Annotated[StrictStr, Field(min_length=1, max_length=80)]
    parentId: Annotated[StrictInt, Field(ge=0)]


class Prepared(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: Digest
    expiresAt: Annotated[StrictInt, Field(gt=0)]
    intent: Intent
    matchedIds: IDS
    preview: Annotated[list[Preview], Field(max_length=10)]
    previewTruncated: StrictBool
    destination: Destination | None


class Arguments(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: Digest
    expiresAt: Annotated[StrictInt, Field(gt=0)]
    intent: Intent


def intent(value):
    return parse(value).model_dump(exclude_none=True)


def plan(value, request):
    prepared = Prepared.model_validate(value)
    ids = prepared.intent.identifiers()
    preview_ids = [row.imageId for row in prepared.preview]
    if (prepared.intent.model_dump(exclude_none=True) != request
            or prepared.matchedIds != ids
            or prepared.expiresAt <= int(time.time())
            or preview_ids != ids[:10]
            or prepared.previewTruncated != (len(ids) > 10)):
        raise ValueError("gallery preparation differs from its fixed selection or intent")
    destination = prepared.destination
    if prepared.intent.action == "move":
        if destination is None or destination.id != prepared.intent.folderId:
            raise ValueError("gallery destination differs from the request")
    elif destination is not None:
        raise ValueError("unexpected gallery destination")
    result = prepared.model_dump()
    result["intent"] = prepared.intent.model_dump(exclude_none=True)
    return result


class Outcome(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["move", "favorite", "metadata", "delete"]
    changed: StrictBool
    beforeVersion: Digest
    matchedIds: IDS
    changedIds: Annotated[list[Identifier], Field(max_length=100)]
    cleanupPendingIds: Annotated[list[Identifier], Field(max_length=100)]


def outcome(value, args):
    result = Outcome.model_validate(value)
    requested = parse(args["intent"])
    ids = requested.identifiers()
    if (result.action != requested.action or result.beforeVersion != args["version"]
            or result.matchedIds != ids
            or result.changedIds != sorted(set(result.changedIds))
            or not set(result.changedIds) <= set(ids)
            or result.changed != bool(result.changedIds)):
        raise ValueError("unverified gallery mutation scope or change facts")
    if requested.action == "delete":
        if result.changedIds != ids or result.cleanupPendingIds != ids:
            raise ValueError("the complete approved deletion was not durably queued")
    elif result.cleanupPendingIds:
        raise ValueError("metadata operations cannot enqueue storage deletion")
    return result.model_dump()
