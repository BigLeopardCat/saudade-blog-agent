"""In-process stream controls; only HTTP adapters serialize their payloads."""

from dataclasses import dataclass
from typing import Literal

EventName = Literal[
    "progress", "confirmation", "pending", "command", "receipts", "task_input", "text_reset"
]


@dataclass(frozen=True)
class StreamEvent:
    name: EventName
    data: dict | list[dict] | str

    def __post_init__(self):
        if self.name == "progress":
            valid = isinstance(self.data, str)
        elif self.name == "receipts":
            valid = isinstance(self.data, list) and all(isinstance(r, dict) for r in self.data)
        elif self.name in {"confirmation", "pending", "command", "task_input", "text_reset"}:
            valid = isinstance(self.data, dict)
        else:
            valid = False
        if not valid:
            raise ValueError("Invalid stream control event")
