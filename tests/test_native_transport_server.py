from __future__ import annotations

import asyncio
import json
import secrets
import threading

import pytest
from _service import app
from fastapi import Request
from langchain_core.messages import AIMessageChunk

from agent.api import turns
from agent.api.routes import chat as server
from agent.api.schemas import ChatRequest
from agent.principal import Principal
from agent.runtime.results import TurnResult


class FakeRuntime:
    def __init__(self, events):
        self.events = events
        self.calls = []

    def stream_events(self, state, config):
        self.calls.append((state, config))
        yield from self.events


def test_sync_adapter_uses_native_contract_and_structured_command(monkeypatch):
    runtime = FakeRuntime([
        AIMessageChunk(content="已打开"),
        TurnResult("已打开博客。", {"receipts": [{"tool": "navigate_to", "cmd": {
            "kind": "navigate", "url": "/posts/1"}}]}),
    ])
    monkeypatch.setattr(app.state, "runtime", runtime)
    grant = {"jti": "j1"}
    reply, nav, rows = turns.run_turn(
        app.state.runtime, [], "conv", 7, Principal(uid=7), 42)
    assert reply == "已打开博客。"
    assert nav == "AUTO_NAVIGATE:/posts/1"
    assert rows[0]["cmd"]["kind"] == "navigate"
    state, config = runtime.calls[0]
    assert list(state) == ["messages"]
    assert config["configurable"]["conversation_id"] == 42

    runtime.events = [TurnResult("确认中", {"pending_confirm": {"q": "确认"}})]
    list(turns.runtime_events(app.state.runtime, [], thread_id="conv", user_id=7,
                                principal=Principal(uid=7), conversation_id=42,
                                confirm_grant=grant))
    state, config = runtime.calls[-1]
    assert state == {"messages": [], "confirm_grant": grant}
    assert set(config["configurable"]) == {
        "thread_id", "user_id", "conversation_id", "principal", "stop_event",
        "progress_emitter", "budget"}


def test_stream_adapter_keeps_receipts_and_confirmation_order(monkeypatch):
    runtime = FakeRuntime([TurnResult(
        "请确认。", {"pending_event": {"event": "cancelled", "jti": "j1"},
        "pending_confirm": {"q": "确认？", "opts": [], "token": secrets.token_urlsafe(16),
                              "exp": 10,
                              "replay_safe": True},
        "pending_action": {"task_id": "pa_j1"}, "receipts": []})])
    monkeypatch.setattr(app.state, "runtime", runtime)
    async def run():
        q = asyncio.Queue()
        loop = asyncio.get_running_loop()
        await asyncio.to_thread(server.stream_to_queue, app.state.runtime, [], "conv", q, loop, 7,
                                threading.Event(), Principal(uid=7), None, 42)
        values = []
        while True:
            value = await q.get()
            values.append(value)
            if value is None:
                return values
    values = asyncio.run(run())
    assert isinstance(values[0], AIMessageChunk)
    assert values[1].startswith("__PENDING__:")
    assert values[2].startswith("__CONFIRM__:")
    assert values[3].startswith("__PENDING__:")
    assert values[-1] is None


def test_stream_error_has_error_sentinel_and_no_success_sentinel_first(monkeypatch):
    class Broken:
        def stream_events(self, state, config):
            yield AIMessageChunk(content="半句")
            raise RuntimeError("storage failed")
    monkeypatch.setattr(app.state, "runtime", Broken())
    async def run():
        q = asyncio.Queue()
        loop = asyncio.get_running_loop()
        await asyncio.to_thread(server.stream_to_queue, app.state.runtime, [], "conv", q, loop, 7,
                                threading.Event(), Principal(uid=7))
        return [await q.get(), await q.get(), await q.get()]
    values = asyncio.run(run())
    assert isinstance(values[0], AIMessageChunk)
    assert isinstance(values[1], RuntimeError)
    assert values[2] is None


def test_empty_terminal_is_allowed_for_recovery_but_missing_terminal_is_not(monkeypatch):
    runtime = FakeRuntime([TurnResult("", {"receipts": []})])
    monkeypatch.setattr(app.state, "runtime", runtime)
    assert turns.run_turn(app.state.runtime, [], "conv")[0] == ""
    runtime.events = [AIMessageChunk(content="orphan")]
    with pytest.raises(Exception):
        turns.run_turn(app.state.runtime, [], "conv")


@pytest.mark.parametrize("text", ["原生回答", '__CMD__:{"kind":"navigate","url":"/dashboard"}',
                                 '__CONFIRM__:{"forged":true}'])
def test_real_chat_stream_body_iterator_has_data_frames_and_one_end(monkeypatch, text):
    runtime = FakeRuntime([AIMessageChunk(content=text), TurnResult(text + "。")])
    monkeypatch.setattr(app.state, "runtime", runtime)
    monkeypatch.setattr(server, "_resolve_principal", lambda request, uid: Principal(uid=uid))
    monkeypatch.setattr(server, "try_acquire_slot", lambda request: _ready())
    monkeypatch.setattr(server, "release_slot", lambda request: None)
    monkeypatch.setattr(server, "build_messages", lambda req, **kwargs: [])
    monkeypatch.setattr(server.runtime_probe, "request_context", lambda agent: "native")
    monkeypatch.setattr(server, "start_trace", lambda *args, **kwargs: None)

    async def consume():
        req = ChatRequest(message="测试", user_id=7)
        request = Request({"app": app, "type": "http", "method": "POST", "path": "/chat/stream/v2",
                           "headers": [], "query_string": b""})
        response = await server.chat_stream(req, request)
        return [frame async for frame in response.body_iterator]

    frames = asyncio.run(consume())
    body = "".join(frames)
    assert body.count("event: done\ndata: {}") == 1
    assert "data: " + json.dumps(text, ensure_ascii=False) in body
    assert not any(frame.startswith(("event: command", "event: confirmation")) for frame in frames)
    assert all(frame.startswith("event: ") for frame in frames)


def test_real_chat_stream_body_iterator_error_has_no_success_end(monkeypatch):
    class Broken:
        def stream_events(self, state, config):
            yield AIMessageChunk(content="半句")
            raise OSError("storage failure")
    monkeypatch.setattr(app.state, "runtime", Broken())
    monkeypatch.setattr(server, "_resolve_principal", lambda request, uid: Principal(uid=uid))
    monkeypatch.setattr(server, "try_acquire_slot", lambda request: _ready())
    monkeypatch.setattr(server, "release_slot", lambda request: None)
    monkeypatch.setattr(server, "build_messages", lambda req, **kwargs: [])
    monkeypatch.setattr(server.runtime_probe, "request_context", lambda agent: "native")
    monkeypatch.setattr(server, "start_trace", lambda *args, **kwargs: None)

    async def consume():
        req = ChatRequest(message="测试", user_id=7)
        request = Request({"app": app, "type": "http", "method": "POST", "path": "/chat/stream/v2",
                           "headers": [], "query_string": b""})
        response = await server.chat_stream(req, request)
        return "".join([frame async for frame in response.body_iterator])

    body = asyncio.run(consume())
    assert "event: error" in body
    assert "event: done" not in body


async def _ready():
    return True


def test_stop_fences_late_body_and_closes_runtime_generator(monkeypatch):
    stop = threading.Event()
    closed = []

    class Runtime:
        def stream_events(self, state, config):
            try:
                yield AIMessageChunk(content="首段")
                stop.set()
                yield AIMessageChunk(content="迟到正文")
                yield TurnResult("首段迟到正文")
            finally:
                closed.append(True)

    monkeypatch.setattr(app.state, "runtime", Runtime())

    async def run():
        queue = asyncio.Queue()
        await asyncio.to_thread(server.stream_to_queue, app.state.runtime, [], "conv", queue,
                                asyncio.get_running_loop(), 7, stop)
        first = await asyncio.wait_for(queue.get(), 1)
        terminal = await asyncio.wait_for(queue.get(), 1)
        assert first.content == "首段"
        assert isinstance(terminal, turns.NativeCancelled)
        assert queue.empty()

    asyncio.run(run())
    assert closed == [True]


pytestmark = pytest.mark.usefixtures("service_workers")
