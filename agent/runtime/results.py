"""A terminal reply and optional existing execution-protocol updates."""

from dataclasses import dataclass


@dataclass(frozen=True)
class TurnResult:
    reply: str
    execution: dict | None = None
