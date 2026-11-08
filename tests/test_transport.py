from __future__ import annotations

import pytest
from langchain_core.messages import AIMessageChunk

from agent.contracts.events import StreamEvent
from agent.runtime.results import ReplyReset, TurnResult
from agent.transport import (
    TransportProtocolError,
    collect_turn,
    iter_transport_events,
    validate_command,
)


def test_stream_chunks_and_terminal_result_do_not_duplicate_body():
    events = iter_transport_events([
        AIMessageChunk(content="你好"),
        AIMessageChunk(content="，泠月喵。"),
        TurnResult("你好，泠月喵。", {"receipts": []}),
    ])
    chunks = [event for event in events if isinstance(event, AIMessageChunk)]
    assert "".join(chunk.content for chunk in chunks) == "你好，泠月喵。"


def test_reply_reset_withdraws_only_prose_and_preserves_factual_receipts():
    source = [AIMessageChunk(content="无效正文"), ReplyReset(),
              AIMessageChunk(content="纠正后的正文"),
              TurnResult("纠正后的正文", {"receipts": [{"tool": "write", "result": "ok"}]})]
    stream = list(iter_transport_events(source))
    assert StreamEvent("text_reset", {}) in stream
    assert any(isinstance(e, StreamEvent) and e.name == "receipts" for e in stream)
    collected = collect_turn(source)
    assert collected.reply == "纠正后的正文"
    assert [e.data for e in collected.events if e.name == "receipts"] == [
        [{"tool": "write", "result": "ok"}]]
    with pytest.raises(TransportProtocolError):
        list(iter_transport_events([TurnResult("done"), ReplyReset()]))


def test_terminal_only_confirmation_is_emitted_once_with_receipt_command():
    events = list(iter_transport_events([TurnResult(
        "已准备好，请确认。",
        {"pending_confirm": {"id": "c1", "text": "已准备好，请确认。"},
         "confirm_text": "已准备好，请确认。",
         "receipts": [{"tool": "navigate_to", "cmd": {"kind": "navigate", "url": "/x"}}]},
    )]))
    assert sum(isinstance(event, AIMessageChunk) for event in events) == 1
    assert (
        sum((isinstance(event, StreamEvent) and event.name == "confirmation") for event in events)
        == 1
    )
    assert (
        sum((isinstance(event, StreamEvent) and event.name == "command") for event in events) == 1
    )
    assert (
        sum((isinstance(event, StreamEvent) and event.name == "receipts") for event in events) == 1
    )
    pending = [event.data for event in events
               if (isinstance(event, StreamEvent) and event.name == "pending")]
    assert pending == []
    popup = [event.data for event in events
             if (isinstance(event, StreamEvent) and event.name == "confirmation")][0]
    assert set(popup) == {"id", "q", "opts", "token", "exp", "replay_safe",
                          "server_time", "notices"}
    assert popup["notices"] == []
    assert isinstance(popup["server_time"], int)
    assert popup["q"] == ""


def test_cancel_and_noop_are_execution_facts_without_duplicate_terminal_text():
    result = collect_turn([TurnResult(
        "没有执行改动。", {"noop_note": "没有执行改动。", "receipts": []})])
    assert result.reply == "没有执行改动。"
    assert result.events == []


@pytest.mark.parametrize("command", [
    {"kind": "navigate", "url": "/a"},
    {"kind": "navigate", "mode": "confirm", "url": "/a"},
    {"kind": "effect", "effect": "rain", "action": "off"},
    {"kind": "darkmode", "mode": "on"},
    {"kind": "appearance", "settings": {"rain": "off"}},
    {"kind": "article_editor", "operation": "read"},
])
def test_transport_accepts_supported_control_shapes(command):
    validate_command(command)


@pytest.mark.parametrize("command", [None, {}, {"kind": "unknown"},
    {"kind": "navigate", "url": {}}, {"kind": "appearance", "settings": "on"}])
def test_transport_rejects_malformed_controls(command):
    with pytest.raises(TransportProtocolError):
        validate_command(command)


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


@pytest.mark.parametrize("client_action", [None, {"kind": "navigate", "url": "/about"}])
def test_all_receipts_validate_before_dispatching_any_command(client_action):
    stream = iter_transport_events([TurnResult("", {"client_action": client_action, "receipts": [
        {"cmd": {"kind": "navigate", "url": "/about"}},
        {"cmd": {"kind": "unsupported"}},
    ]})])
    with pytest.raises(TransportProtocolError):
        next(stream)


@pytest.mark.parametrize("name,data", [
    ("confirmation", "not an object"), ("receipts", {}), ("receipts", ["invalid row"]),
    ("progress", {}), ("unknown", {}),
])
def test_control_event_rejects_invalid_envelopes(name, data):
    with pytest.raises(ValueError):
        StreamEvent(name, data)


def test_sync_keeps_all_ordered_commands_and_confirmation_separate_from_model_text():
    receipts = [{"tool": "navigate_page", "cmd": {"kind": "navigate", "url": "/x"}},
                {"tool": "set_page_appearance", "cmd": {
                    "kind": "appearance", "settings": {"rain": "off"}}}]
    literal = "AUTO_NAVIGATE:/not-a-command"
    result = collect_turn([TurnResult(literal, {
        "receipts": receipts, "pending_confirm": {"q": "确认？", "opts": []}})])
    assert result.reply == literal
    assert [e.name for e in result.events] == ["command", "command", "receipts", "confirmation"]
    assert [e.data for e in result.events[:2]] == [r["cmd"] for r in receipts]
    assert result.events[2].data == receipts
