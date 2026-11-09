"""In-process stream controls; only HTTP adapters serialize their payloads."""

from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictStr, model_validator

EventName = Literal[
    "progress", "confirmation", "pending", "command", "receipts", "task_input", "text_reset"
]


class ProgressWork(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: StrictStr = Field(min_length=1, max_length=128)
    stage: StrictStr = Field(min_length=1, max_length=64)
    status: Literal["running", "waiting", "completed", "failed", "cancelled", "unknown"]


class ProgressUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: StrictStr = Field(min_length=1, max_length=300)
    work: ProgressWork


class ConfirmationNotice(BaseModel):
    """Server-owned operation guidance, separate from user-authored review content."""

    model_config = ConfigDict(extra="forbid")
    kind: Literal["info", "warning"]
    text: StrictStr = Field(min_length=1, max_length=1000)


class ConfirmationField(BaseModel):
    model_config = ConfigDict(extra="forbid")
    label: StrictStr = Field(min_length=1, max_length=80)
    value: StrictStr = Field(max_length=16000)


class ConfirmationSection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    label: StrictStr = Field(min_length=1, max_length=80)
    text: StrictStr = Field(max_length=64000)
    format: Literal["plain", "markdown"] = "plain"


class ConfirmationDetails(BaseModel):
    """Metadata stays literal; only explicitly authored rich text uses Markdown."""

    model_config = ConfigDict(extra="forbid")
    fields: list[ConfirmationField] = Field(default_factory=list, max_length=24)
    sections: list[ConfirmationSection] = Field(default_factory=list, max_length=8)

    @model_validator(mode="after")
    def bounded(self):
        if (
            sum(len(row.value) for row in self.fields) + sum(len(row.text) for row in self.sections)
            > 128000
        ):
            raise ValueError("confirmation content exceeds its display budget")
        return self


def confirmation_text(card):
    """Project the actual card for evaluation without promoting drafts into facts."""
    parts = [card.get("q", "")]
    if "details" in card:
        details = ConfirmationDetails.model_validate(card["details"])
        parts.extend(f"{row.label}：{row.value}" for row in details.fields)
        parts.extend(f"{row.label}：\n{row.text}" for row in details.sections)
    return "\n".join(parts)


@dataclass(frozen=True)
class StreamEvent:
    name: EventName
    data: dict | list[dict] | str

    def __post_init__(self):
        if self.name == "progress":
            valid = isinstance(self.data, str)
            if isinstance(self.data, dict):
                ProgressUpdate.model_validate(self.data)
                valid = True
        elif self.name == "receipts":
            valid = isinstance(self.data, list) and all(isinstance(r, dict) for r in self.data)
        elif self.name in {"confirmation", "pending", "command", "task_input", "text_reset"}:
            valid = isinstance(self.data, dict)
        else:
            valid = False
        if not valid:
            raise ValueError("Invalid stream control event")
