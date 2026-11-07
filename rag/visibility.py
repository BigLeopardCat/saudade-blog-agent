"""Validate cached candidates against the source's current public document versions."""

from dataclasses import dataclass
from typing import Annotated, Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr, model_validator

Digest = Annotated[StrictStr, Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")]
Identifier = Annotated[StrictInt, Field(gt=0, le=2_147_483_647)]


class VisibilityUnavailable(RuntimeError):
    """Unknown source authorization or version facts cannot authorize a cached candidate."""

    def __init__(self, reason):
        self.reason = reason
        super().__init__(reason)


class DocumentRef(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    type: Literal["note"]
    id: Identifier
    revision: Digest


class DocumentState(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    type: Literal["note"]
    id: Identifier
    visible: StrictBool
    contentRevision: Digest | None

    @model_validator(mode="after")
    def visible_revision(self):
        if self.visible != (self.contentRevision is not None):
            raise ValueError("Visible documents require a revision; hidden documents expose none")
        return self


class Snapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    schemaVersion: Annotated[StrictInt, Field(ge=1, le=1)]
    documents: Annotated[list[DocumentState], Field(min_length=1, max_length=64)]


@dataclass(frozen=True)
class Candidates:
    current: list[dict]
    changed: list[dict]


def current_public(candidates: list[dict], get) -> Candidates:
    """Verify all identities once, independently of response order; never guess missing facts."""
    if not candidates:
        return Candidates([], [])
    if len(candidates) > 64:
        raise VisibilityUnavailable("candidate_limit_exceeded")
    if any(not isinstance(row, dict) or not row.get("content_revision") for row in candidates):
        raise VisibilityUnavailable("missing_source_revision")
    try:
        refs = [DocumentRef.model_validate({"type": row["type"], "id": row["id"],
                                           "revision": row["content_revision"]})
                for row in candidates]
    except (KeyError, TypeError, ValueError):
        raise VisibilityUnavailable("invalid_candidate_identity") from None
    identities = {(ref.type, ref.id) for ref in refs}
    if len(identities) != len(refs):
        raise VisibilityUnavailable("duplicate_candidate_identity")
    try:
        payload = {"documents": [ref.model_dump() for ref in refs]}
        reply = get("/documents/validate", payload=payload)
        snapshot = Snapshot.model_validate(reply)
    except (httpx.HTTPError, TypeError, ValueError):
        # Transport or validation failures carry no authoritative absence or permission facts.
        raise VisibilityUnavailable("source_visibility_unavailable") from None
    actual = {(row.type, row.id): row for row in snapshot.documents}
    if len(actual) != len(snapshot.documents) or set(actual) != identities:
        raise VisibilityUnavailable("incomplete_source_visibility")
    current: list[dict] = []
    changed: list[dict] = []
    for candidate, ref in zip(candidates, refs):
        state = actual[(ref.type, ref.id)]
        target = current if state.visible and state.contentRevision == ref.revision else changed
        target.append(candidate)
    return Candidates(current, changed)
