"""Translate the native runtime contract to the existing chat protocols.

The runtime yields only ``AIMessageChunk`` objects and one terminal ``TurnResult``.
This module owns the wire details (SSE control frames and the legacy JSON response)
without inspecting planner nodes, graph state, or model metadata.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Callable, Iterable, Iterator

from langchain_core.messages import AIMessageChunk

from agent.runtime.results import TurnResult
from agent.runtime.confirmation import frames as confirmation_frames


class TransportProtocolError(RuntimeError):
    """The runtime violated the stream_events contract."""


@dataclass(frozen=True)
class CollectedTurn:
    reply: str
    navigation: str
    execution: list[dict]


def _json_frame(prefix: str, value) -> str:
    return prefix + json.dumps(value, ensure_ascii=False)



def command_wire(command: dict) -> str:
    """Encode a receipt command for the synchronous response contract."""
    kind = command.get("kind") if isinstance(command, dict) else None
    if kind == "navigate" and command.get("url"):
        prefix = "NAVIGATE:" if str(command.get("mode") or "") == "confirm" else "AUTO_NAVIGATE:"
        return prefix + str(command["url"])
    if kind == "effect" and command.get("effect"):
        return f"EFFECT:{command['effect']}:{command.get('action', 'on')}"
    if kind == "darkmode" and command.get("mode"):
        return "DARKMODE:" + str(command["mode"])
    if kind:
        raise TransportProtocolError(f"unsupported receipt command kind: {kind}")
    raise TransportProtocolError("receipt command kind is missing")


def _execution_payload(execution: dict | None) -> dict:
    if execution is None:
        return {}
    if not isinstance(execution, dict):
        raise TransportProtocolError("TurnResult.execution must be an object")
    return execution


def _execution_frames(execution: dict | None) -> Iterator[str]:
    execution = _execution_payload(execution)
    if not execution:
        return
    receipts = execution.get("receipts") or []
    if not isinstance(receipts, list):
        raise TransportProtocolError("execution.receipts must be a list")
    for receipt in receipts:
        if not isinstance(receipt, dict):
            raise TransportProtocolError("execution receipt must be an object")
        command = receipt.get("cmd")
        if command is not None:
            if not isinstance(command, dict):
                raise TransportProtocolError("execution receipt cmd is invalid")
            command_wire(command)
            yield _json_frame("__CMD__:", command)
    if receipts:
        yield _json_frame("__EXEC__:", receipts)
    # Confirmation framing owns its field allow-list and cancellation ordering.
    yield from confirmation_frames(execution)


def _terminal_delta(visible: str, reply: str) -> str:
    """Return only terminal text not already emitted by chunks.

    A runtime must return the aggregate visible reply. If it cannot be related to
    the emitted prefix, silently sending the aggregate would duplicate the answer;
    fail closed instead.
    """
    if not reply:
        if visible:
            raise TransportProtocolError("terminal reply is empty after visible chunks")
        return ""
    if not visible:
        return reply
    if reply == visible or reply.startswith(visible):
        return reply[len(visible):]
    raise TransportProtocolError("terminal reply is inconsistent with visible chunks")


def iter_transport_events(events: Iterable[AIMessageChunk | TurnResult]) -> Iterator[str | AIMessageChunk]:
    """Yield visible chunks and protocol frames, exactly once per event.

    The final ``TurnResult.reply`` is used only to fill a missing suffix. It is
    never emitted as a second copy of the streamed body.
    """
    visible = ""
    terminal: TurnResult | None = None
    for event in events:
        if terminal is not None:
            raise TransportProtocolError("runtime yielded events after TurnResult")
        if isinstance(event, AIMessageChunk):
            content = str(event.content or "")
            if content:
                visible += content
                yield event
            continue
        if isinstance(event, TurnResult):
            if terminal is not None:
                raise TransportProtocolError("runtime yielded more than one TurnResult")
            terminal = event
            continue
        raise TransportProtocolError(f"unsupported runtime event: {type(event).__name__}")
    if terminal is None:
        raise TransportProtocolError("runtime ended without TurnResult")
    suffix = _terminal_delta(visible, str(terminal.reply or ""))
    if suffix:
        visible += suffix
        yield AIMessageChunk(content=suffix)
    yield from _execution_frames(terminal.execution)


def collect_turn(events: Iterable[AIMessageChunk | TurnResult]) -> CollectedTurn:
    """Collect a native turn for the non-streaming HTTP response."""
    visible = ""
    terminal: TurnResult | None = None
    for event in events:
        if terminal is not None:
            raise TransportProtocolError("runtime yielded events after TurnResult")
        if isinstance(event, AIMessageChunk):
            visible += str(event.content or "")
        elif isinstance(event, TurnResult):
            if terminal is not None:
                raise TransportProtocolError("runtime yielded more than one TurnResult")
            terminal = event
        else:
            raise TransportProtocolError(f"unsupported runtime event: {type(event).__name__}")
    if terminal is None:
        raise TransportProtocolError("runtime ended without TurnResult")
    reply = str(terminal.reply or "")
    _terminal_delta(visible, reply)
    execution = _execution_payload(terminal.execution)
    list(_execution_frames(execution))
    navigation = ""
    for receipt in execution.get("receipts", []):
        command = receipt.get("cmd") if isinstance(receipt, dict) else None
        if isinstance(command, dict):
            wire = command_wire(command)
            if wire:
                navigation = wire
    return CollectedTurn(reply=reply, navigation=navigation,
                         execution=list(execution.get("receipts") or []))


def emit_to_queue(events: Iterable[AIMessageChunk | TurnResult], put: Callable[[str | AIMessageChunk], None]) -> None:
    """Adapt runtime events to a thread-safe queue callback."""
    for event in iter_transport_events(events):
        put(event)
