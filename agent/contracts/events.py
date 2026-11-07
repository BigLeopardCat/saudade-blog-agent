"""In-process stream controls; only HTTP adapters serialize their payloads."""

from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictStr

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
