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
    field_validator,
    model_validator,
)

from agent.domains.gallery.targets import Digest, Identifier, Patch

PATH = "/api/protect/gallery/image-actions"
IDS = Annotated[list[Identifier], Field(min_length=1, max_length=100)]


class Item(BaseModel):
    model_config = ConfigDict(extra="forbid")
    imageId: Identifier
    changes: Patch

    @field_validator("changes", mode="before")
    @classmethod
    def no_null_changes(cls, value):
        if isinstance(value, dict) and any(item is None for item in value.values()):
            raise ValueError("omit unchanged fields; explicit null is not a gallery change")
        return value


class Intent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["move", "favorite", "metadata", "delete"]
    imageIds: IDS | None = None
    folderId: Annotated[StrictInt, Field(ge=0)] | None = None
    isFavorite: StrictBool | None = None
    items: Annotated[list[Item], Field(min_length=1, max_length=100)] | None = None

    @model_validator(mode="before")
    @classmethod
    def no_null_fields(cls, value):
        if isinstance(value, dict) and any(item is None for item in value.values()):
            raise ValueError("gallery action fields cannot be null")
        return value

    @model_validator(mode="after")
    def normalize(self):
        if self.action == "metadata":
            if self.items is None or any(value is not None for value in (
                    self.imageIds, self.folderId, self.isFavorite)):
                raise ValueError("metadata requires only per-image items")
            ids = [item.imageId for item in self.items]
            self.items = sorted(self.items, key=lambda item: item.imageId)
        else:
            if self.imageIds is None or self.items is not None:
                raise ValueError("this action requires fixed imageIds")
            ids = self.imageIds
            self.imageIds = sorted(ids)
            if ((self.action == "move" and (self.folderId is None or self.isFavorite is not None))
                    or (self.action == "favorite" and (
                        self.isFavorite is None or self.folderId is not None))
                    or (self.action == "delete" and (
                        self.folderId is not None or self.isFavorite is not None))):
                raise ValueError("fields do not match the gallery action")
        if len(ids) != len(set(ids)):
            raise ValueError("image IDs must be unique")
        encoded = json.dumps(self.model_dump(exclude_none=True), ensure_ascii=False,
                             separators=(",", ":")).encode()
        if len(encoded) > 8192:
            raise ValueError("gallery intent exceeds 8192 UTF-8 bytes; use a smaller selection")
        return self

    def identifiers(self):
        return [item.imageId for item in self.items] if self.items is not None else self.imageIds


class Preview(BaseModel):
    model_config = ConfigDict(extra="forbid")
    imageId: Identifier
    displayName: Annotated[StrictStr, Field(max_length=255)] | None
    folderId: Annotated[StrictInt, Field(ge=0)]
    isFavorite: StrictBool


class Destination(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: Identifier
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
    return Intent.model_validate(value).model_dump(exclude_none=True)


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
    if prepared.intent.action == "move" and prepared.intent.folderId:
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
    requested = Intent.model_validate(args["intent"])
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
