"""Adapt runtime outcomes to typed stream controls and synchronous responses.

The runtime yields text deltas, typed input/revision events and one terminal result.
This module owns application event projection and the synchronous JSON response
without inspecting planner nodes, graph state, or model metadata.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Generator, Iterable, Iterator

from langchain_core.messages import AIMessageChunk

from agent.contracts.events import StreamEvent
from agent.products.blog.confirmation import frames as confirmation_frames
from agent.runtime.results import ReplyReset, TurnResult
from agent.runtime.task_inputs import TaskInputEvent


class TransportProtocolError(RuntimeError):
    """The runtime violated the stream_events contract."""


@dataclass(frozen=True)
class CollectedTurn:
    reply: str
    events: list[StreamEvent]


def validate_command(command: dict) -> None:
    """Reject unsupported control shapes without converting them into prose."""
    if not isinstance(command, dict):
        raise TransportProtocolError("receipt command must be an object")
    kind = command.get("kind")
    valid = (
        kind == "navigate" and isinstance(command.get("url"), str) and command["url"]
        or kind == "effect" and isinstance(command.get("effect"), str) and command["effect"]
        or kind == "darkmode" and isinstance(command.get("mode"), str) and command["mode"]
        or kind == "appearance" and isinstance(command.get("settings"), dict)
        and command["settings"]
        or kind == "article_editor" and command.get("operation") in {
            "read", "propose", "summary", "prepare_publish"}
    )
    if not valid:
        raise TransportProtocolError("unsupported or malformed receipt command")


def _execution_payload(execution: dict | None) -> dict:
    if execution is None:
        return {}
    if not isinstance(execution, dict):
        raise TransportProtocolError("TurnResult.execution must be an object")
    return execution


def _execution_frames(execution: dict | None) -> Iterator[StreamEvent]:
    execution = _execution_payload(execution)
    if not execution:
        return
    commands = []
    client_action = execution.get("client_action")
    if client_action:
        validate_command(client_action)
        commands.append(StreamEvent("command", client_action))
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
            validate_command(command)
            if receipt.get("browser_status") not in {"completed", "failed", "timeout"}:
                commands.append(StreamEvent("command", command))
    yield from commands
    if receipts:
        yield StreamEvent("receipts", receipts)
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


def iter_transport_events(
    events: Iterable[AIMessageChunk | TurnResult | ReplyReset],
) -> Generator[StreamEvent | AIMessageChunk, None, None]:
    """Yield visible chunks and protocol frames, exactly once per event.

    The final ``TurnResult.reply`` is used only to fill a missing suffix. It is
    never emitted as a second copy of the streamed body.
    """
    source = iter(events)
    try:
        yield from _stream_body(source)
    finally:
        close = getattr(source, "close", None)
        if close is not None:
            close()


def _stream_body(events):
    visible = ""
    terminal = None
    for event in events:
        if terminal is not None:
            raise TransportProtocolError("runtime yielded events after TurnResult")
        if isinstance(event, TaskInputEvent):
            yield StreamEvent("task_input", event.data)
            continue
        if isinstance(event, ReplyReset):
            visible = ""
            yield StreamEvent("text_reset", {})
            continue
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


def reply_text(previous, event):
    """Apply a typed delta or trusted producer revision without parsing model text."""
    if isinstance(event, AIMessageChunk):
        return previous + str(event.content or "")
    return "" if isinstance(event, StreamEvent) and event.name == "text_reset" else previous


def collect_turn(events: Iterable[AIMessageChunk | TurnResult | ReplyReset]) -> CollectedTurn:
    """Collect a native turn for the non-streaming HTTP response."""
    reply = ""
    controls = []
    for event in iter_transport_events(events):
        reply = reply_text(reply, event)
        if isinstance(event, StreamEvent) and event.name != "text_reset":
            controls.append(event)
    return CollectedTurn(reply=reply, events=controls)


def emit_to_queue(
    events: Iterable[AIMessageChunk | TurnResult],
    put: Callable[[StreamEvent | AIMessageChunk], None],
) -> None:
    """Adapt runtime events to a thread-safe queue callback."""
    stream = iter_transport_events(events)
    try:
        for event in stream:
            put(event)
    finally:
        stream.close()
