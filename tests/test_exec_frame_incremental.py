"""Native receipts are emitted once after a validated terminal result."""

import json

import pytest
from langchain_core.messages import AIMessageChunk

from agent.runtime.results import TurnResult
from agent.transport import TransportProtocolError, iter_transport_events

RECEIPT = {"tool": "navigate_to", "cmd": {"kind": "navigate", "url": "/about"}}


def test_visible_preface_precedes_completion_and_receipts_are_exactly_once():
    stream = iter_transport_events([
        AIMessageChunk(content="我来处理。"),
        TurnResult("我来处理。指令已发送。", {"receipts": [RECEIPT]}),
    ])
    assert next(stream).content == "我来处理。"
    tail = list(stream)
    assert tail[0].content == "指令已发送。"
    assert len([row for row in tail if str(row).startswith("__CMD__:")]) == 1
    frames = [json.loads(row.removeprefix("__EXEC__:")) for row in tail
              if isinstance(row, str) and row.startswith("__EXEC__:")]
    assert frames == [[RECEIPT]]


def test_failure_before_completion_cannot_dispatch_a_queued_browser_command():
    def events():
        yield AIMessageChunk(content="处理中。")
        raise OSError("model or storage failed before completion")

    stream = iter_transport_events(events())
    assert next(stream).content == "处理中。"
    with pytest.raises(OSError):
        next(stream)


def test_late_events_invalidate_terminal_before_any_receipt_dispatch():
    stream = iter_transport_events([
        TurnResult("指令已发送。", {"receipts": [RECEIPT]}),
        AIMessageChunk(content="非法迟到内容"),
    ])
    with pytest.raises(TransportProtocolError):
        next(stream)


def test_empty_receipts_emit_no_execution_frame():
    events = list(iter_transport_events([TurnResult("没有改动。", {"receipts": []})]))
    assert [item.content for item in events] == ["没有改动。"]
