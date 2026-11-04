"""Browser observations and JSON-only references to durable client operations."""

from typing import Annotated, Literal, TypedDict

from pydantic import BaseModel, ConfigDict, Field, StrictInt, TypeAdapter, with_config


@with_config(ConfigDict(extra="forbid"))
class ClientOperationRef(TypedDict):
    tool: Annotated[str, Field(min_length=1, max_length=100)]
    operation_id: Annotated[str, Field(pattern=r"^[0-9a-f]{32}$")]
    uid: Annotated[StrictInt, Field(gt=0)]
    conv: Annotated[StrictInt, Field(gt=0)]
    ledger_key: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


reference_adapter = TypeAdapter(ClientOperationRef)


class EditorObservation(BaseModel):
    """Working-copy observations; text ranges count Unicode code points."""

    editor_id: str = Field(min_length=1, max_length=64)
    revision: int = Field(strict=True, ge=0, le=9_007_199_254_740_991)
    outcome: Literal["read", "proposed", "updated", "unchanged"]
    title: str | None = Field(default=None, max_length=1000)
    content: str | None = Field(default=None, max_length=24000)
    description: str | None = Field(default=None, max_length=16000)
    offset: int | None = Field(default=None, strict=True, ge=0, le=1_000_000)
    next_offset: int | None = Field(default=None, strict=True, ge=0, le=1_000_000)
    total_chars: int | None = Field(default=None, strict=True, ge=0, le=1_000_000)
    proposal_id: str | None = Field(default=None, max_length=64)

    def matches(self, command: dict) -> bool:
        """Check the final observation against the exact requested draft operation."""
        operation = command.get("operation")
        if operation == "read":
            if (self.outcome != "read" or self.content is None or self.description is None
                    or self.total_chars is None or self.offset != command.get("offset")
                    or type(command.get("limit")) is not int
                    or not 1 <= command["limit"] <= 24000 or self.offset is None
                    or self.offset > self.total_chars):
                return False
            end = min(self.total_chars, self.offset + command["limit"])
            return (len(self.content) == end - self.offset
                    and self.next_offset == (end if end < self.total_chars else None))
        if self.editor_id != command.get("editor_id"):
            return False
        if operation == "propose":
            return (self.outcome == "proposed" and self.revision == command.get("revision")
                    and bool(self.proposal_id))
        if operation != "summary" or self.description != command.get("description"):
            return False
        revision = command.get("revision")
        expected = command.get("expected_revision")
        return (type(revision) is int and self.description is not None
                and type(expected) is int and self.revision == expected
                and ((self.outcome == "updated" and expected == revision + 1)
                     or (self.outcome == "unchanged" and expected == revision)))

