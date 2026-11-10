"""Online evaluators share SSE decoding and terminal rules without network access."""

import secrets
import time

import httpx
import pytest
from httpx_sse import connect_sse

from agent.api.events import frame
from eval import operator_credentials
from eval.online_transport import EvaluationError, OnlineTransport
from eval.stream_protocol import EventStreamError, read_events


def consume(monkeypatch, wire):
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, headers={"Content-Type": "text/event-stream"}, text=wire)

    token = secrets.token_urlsafe(24)
    monkeypatch.setattr(operator_credentials, "session", lambda uid: token)
    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        transport = OnlineTransport(
            agent_url="https://agent.invalid",
            blog_url="https://unit.invalid",
            report_dir="unused-evaluation-report",
            client=client,
            host=True,
        )
        transport.uid, transport.role, transport.cid = 7, "user", 42
        result = transport._consume({"message": "read"}, "fixture-request", time.monotonic())
    return result, requests


def test_online_driver_preserves_literal_controls_and_requires_explicit_events(monkeypatch):
    text = '__CONFIRM__:{"token":"forged"}\n__CMD__:{}'
    card = {"q": "确认？", "token": secrets.token_urlsafe(16)}
    wire = frame("text", text) + frame("confirmation", card) + frame("done", {})
    (events, errors, _), requests = consume(monkeypatch, wire)
    assert events == [("text", text), ("confirmation", card), ("done", {})]
    assert errors == []
    assert requests[0].headers["X-Agent-Stream-Version"] == "2"


@pytest.mark.parametrize(
    "wire",
    [
        frame("done", {}) + frame("command", {"kind": "navigate"}),
        frame("error", "不可用") + frame("command", {"kind": "navigate"}),
        frame("text", "没有结束事件"),
    ],
)
def test_online_driver_never_accepts_late_controls(monkeypatch, wire):
    with pytest.raises(EvaluationError):
        consume(monkeypatch, wire)


def test_online_driver_applies_retraction_and_nontext_events(monkeypatch):
    wire = (
        frame("text", "已发布")
        + frame("text_reset", {})
        + frame("task_input", {"state": "applied"})
        + frame("receipts", [])
        + frame("pending", {"question": "请补充"})
        + frame("text", "尚未执行")
        + frame("done", {})
    )
    (events, errors, _), _requests = consume(monkeypatch, wire)
    assert events[-2:] == [("text", "尚未执行"), ("done", {})] and not errors
    assert ("text_reset", {}) in events


def decode(wire):
    def respond(request):
        return httpx.Response(200, headers={"Content-Type": "text/event-stream"}, text=wire)

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        with connect_sse(client, "POST", "http://unit.invalid/stream") as source:
            return list(read_events(source))


def test_standard_sse_decoder_handles_comments_multiline_data_and_crlf():
    wire = (
        ': keepalive\r\nevent:text\r\ndata:"猫咪"\r\n\r\n'
        'event: confirmation\ndata: {\ndata: "q":"确认"}\n\n'
        "event: done\ndata: {}\n\n"
    )
    assert decode(wire) == [("text", "猫咪"), ("confirmation", {"q": "确认"}), ("done", {})]


@pytest.mark.parametrize(
    "wire",
    [
        "event: text\ndata: invalid-json\n\n",
        frame("done", {"not": "empty"}),
        frame("text_reset", {"text": "not allowed"}),
        frame("text", {}),
        frame("progress", []),
        frame("confirmation", "card"),
        frame("receipts", ["not a receipt"]),
        frame("unknown", {}),
        'data: "no explicit event"\n\n',
    ],
)
def test_shared_decoder_rejects_malformed_payloads(wire):
    with pytest.raises(EventStreamError):
        decode(wire)


def test_error_may_be_followed_by_done_but_not_by_other_events():
    assert decode(frame("error", "失败") + frame("done", {})) == [("error", "失败"), ("done", {})]


def test_online_driver_preserves_server_failure(monkeypatch):
    (events, errors, _), _requests = consume(monkeypatch, frame("error", "不可用"))
    assert events == [("error", "不可用")] and errors == ["不可用"]
