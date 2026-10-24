from __future__ import annotations

import json

import pytest
from langchain_core.messages import AIMessageChunk

from agent.runtime.results import TurnResult
from agent.transport import (
    TransportProtocolError,
    collect_turn,
    command_wire,
    iter_transport_events,
)


def test_stream_chunks_and_terminal_result_do_not_duplicate_body():
    events = iter_transport_events([
        AIMessageChunk(content="你好"),
        AIMessageChunk(content="，泠月喵。"),
        TurnResult("你好，泠月喵。", {"receipts": []}),
    ])
    chunks = [event for event in events if isinstance(event, AIMessageChunk)]
    assert "".join(chunk.content for chunk in chunks) == "你好，泠月喵。"


def test_terminal_only_confirmation_is_emitted_once_with_receipt_command():
    events = list(iter_transport_events([TurnResult(
        "已准备好，请确认。",
        {"pending_confirm": {"id": "c1", "text": "已准备好，请确认。"},
         "confirm_text": "已准备好，请确认。",
         "receipts": [{"tool": "navigate_to", "cmd": {"kind": "navigate", "url": "/x"}}]},
    )]))
    assert sum(isinstance(event, AIMessageChunk) for event in events) == 1
    assert sum(str(event).startswith("__CONFIRM__:") for event in events) == 1
    assert sum(str(event).startswith("__CMD__:") for event in events) == 1
    assert sum(str(event).startswith("__EXEC__:") for event in events) == 1
    pending = [json.loads(str(event).split(":", 1)[1]) for event in events
               if str(event).startswith("__PENDING__:")]
    assert pending == []
    popup = [json.loads(str(event).split(":", 1)[1]) for event in events
             if str(event).startswith("__CONFIRM__:")][0]
    assert set(popup) == {"id", "q", "opts", "token", "exp", "replay_safe"}
    assert popup["q"] == ""


def test_cancel_and_noop_are_execution_facts_without_duplicate_terminal_text():
    result = collect_turn([TurnResult(
        "没有执行改动。", {"noop_note": "没有执行改动。", "receipts": []})])
    assert result.reply == "没有执行改动。"
    assert result.execution == []


def test_command_wire_keeps_existing_browser_command_shapes():
    assert command_wire({"kind": "navigate", "url": "/a"}) == "AUTO_NAVIGATE:/a"
    assert command_wire({"kind": "navigate", "mode": "confirm", "url": "/a"}) == "NAVIGATE:/a"
    assert command_wire({"kind": "effect", "effect": "rain", "action": "off"}) == "EFFECT:rain:off"
    assert command_wire({"kind": "darkmode", "mode": "on"}) == "DARKMODE:on"
    with pytest.raises(TransportProtocolError):
        command_wire({"kind": "unknown"})


def test_sync_and_stream_reject_the_same_malformed_receipt():
    events = [TurnResult("答复", {"receipts": [{"cmd": {"kind": "future"}}]})]
    with pytest.raises(TransportProtocolError):
        list(iter_transport_events(events))
    with pytest.raises(TransportProtocolError):
        collect_turn(events)


def test_malformed_execution_shape_fails_closed_in_both_paths():
    events = [TurnResult("答复", ["不是对象"])]
    with pytest.raises(TransportProtocolError):
        list(iter_transport_events(events))
    with pytest.raises(TransportProtocolError):
        collect_turn(events)


def test_missing_terminal_or_inconsistent_reply_fails_closed():
    with pytest.raises(TransportProtocolError):
        list(iter_transport_events([AIMessageChunk(content="部分")]))
    with pytest.raises(TransportProtocolError):
        list(iter_transport_events([AIMessageChunk(content="部分"), TurnResult("另一段")]))
    with pytest.raises(TransportProtocolError):
        list(iter_transport_events([AIMessageChunk(content="完整"), TurnResult("完整"),
                                    AIMessageChunk(content="迟到")]))
    with pytest.raises(TransportProtocolError):
        list(iter_transport_events([AIMessageChunk(content="可见"), TurnResult("")]))


def test_error_before_terminal_does_not_emit_success():
    def broken():
        yield AIMessageChunk(content="部分")
        raise RuntimeError("worker failed")
    with pytest.raises(RuntimeError):
        list(iter_transport_events(broken()))


def test_consumer_failure_closes_a_retained_runtime_generator():
    from agent.transport import emit_to_queue
    closed = []

    def events():
        try:
            yield AIMessageChunk(content="开头")
            yield TurnResult("开头")
        finally:
            closed.append(True)

    source = events()

    def fail(event):
        raise OSError("consumer closed")

    with pytest.raises(OSError):
        emit_to_queue(source, fail)
    assert closed == [True]


def test_explicit_close_releases_upstream_runtime():
    closed = []

    def events():
        try:
            yield AIMessageChunk(content="开头")
            yield TurnResult("开头")
        finally:
            closed.append(True)

    source = events()
    transport = iter_transport_events(source)
    assert next(transport).content == "开头"
    transport.close()
    assert closed == [True]


def test_all_receipts_validate_before_dispatching_any_command():
    stream = iter_transport_events([TurnResult("", {"receipts": [
        {"cmd": {"kind": "navigate", "url": "/about"}},
        {"cmd": {"kind": "unsupported"}},
    ]})])
    with pytest.raises(TransportProtocolError):
        next(stream)
