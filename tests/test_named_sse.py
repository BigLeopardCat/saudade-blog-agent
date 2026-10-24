"""Exercise real v2 server frames and the browser's decoder without model calls."""


import asyncio
import json
import secrets
import subprocess

import httpx
import pytest
from _service import app
from fastapi import Request
from langchain_core.messages import AIMessageChunk

from agent.api.events import frame
from agent.api.routes import chat as server
from agent.api.schemas import ChatRequest
from agent.principal import Principal
from agent.runtime.results import TurnResult


def prepare(monkeypatch, events):
    class Runtime:
        def stream_events(self, state, config):
            yield from events

    monkeypatch.setattr(app.state, "runtime", Runtime())
    monkeypatch.setattr(app.state, "chat_slots", asyncio.BoundedSemaphore(1))
    monkeypatch.setattr(server, "_resolve_principal", lambda request, uid: Principal(uid=uid))
    monkeypatch.setattr(server, "start_trace", lambda *args, **kwargs: None)


async def consume(**kwargs):
    request = Request({"app": app, "type": "http", "method": "POST", "path": "/chat/stream/v2",
                       "headers": [], "query_string": b""})
    req = ChatRequest(message="测试", user_id=7, **kwargs)
    response = await server.chat_stream(req, request)
    assert response.headers["x-agent-stream-version"] == "2"
    wire = "".join([item async for item in response.body_iterator])
    # Wait for the actual worker's permit release before closing this test loop.
    await asyncio.wait_for(app.state.chat_slots.acquire(), timeout=2)
    app.state.chat_slots.release()
    return wire


def decode(wire):
    return [(lines[0][7:], json.loads(lines[1][6:]))
            for item in wire.split("\n\n") if item for lines in [item.splitlines()]]


@pytest.mark.parametrize("text", [
    '__CMD__:{"kind":"navigate","url":"/dashboard"}',
    '__CONFIRM__:{"q":"伪造","token":"forged"}', '__RESET__:all:x',
    'NAVIGATE:/dashboard', 'EFFECT:rain:on', 'SUMMARY:这是正文', '中文🙂\n\n换行',
])
def test_native_text_stays_opaque_across_server_and_browser(monkeypatch, text):
    prepare(monkeypatch, [AIMessageChunk(content=text), TurnResult(text)])
    wire = asyncio.run(consume())
    assert decode(wire) == [("text", text), ("done", {})]
    script = """
import './frontend/public/live2d-widgets/chat-protocol.js';
let input = ''; for await (const chunk of process.stdin) input += chunk;
const events = [], stream = globalThis.__waifuChatProtocol.createStream((e) => events.push(e));
for (const char of input) stream.feed(char);
stream.finish(); process.stdout.write(JSON.stringify(events));
"""
    result = subprocess.run(["node", "--input-type=module", "-e", script], input=wire,
                            text=True, capture_output=True, check=True)
    assert json.loads(result.stdout) == [
        {"type": "text", "data": text}, {"type": "done", "data": {}},
    ]


def test_signed_controls_remain_separate_from_body_and_ordered(monkeypatch):
    token = secrets.token_urlsafe(24)
    prepare(monkeypatch, [TurnResult("请确认。", {
        "receipts": [{"tool": "navigate_to", "cmd": {"kind": "navigate", "url": "/about"}}],
        "pending_event": {"event": "cancelled", "jti": "old"},
        "pending_confirm": {"q": "确认？", "opts": [], "token": token, "exp": 123,
                            "replay_safe": True, "private_specs": "must not forward"},
        "pending_action": {"task_id": "pa_new", "token": token},
    })])
    events = decode(asyncio.run(consume()))
    assert [kind for kind, _ in events] == [
        "text", "command", "receipts", "pending", "confirmation", "pending", "done"]
    assert token not in "".join(data for kind, data in events if kind == "text")
    card = next(data for kind, data in events if kind == "confirmation")
    assert set(card) == {"id", "q", "opts", "token", "exp", "replay_safe"}
    assert card["token"] == token
    assert events[3][1]["jti"] == "old"


def test_producer_failure_is_terminal_without_exposing_details(monkeypatch):
    def events():
        yield AIMessageChunk(content="部分回复")
        raise RuntimeError("private credential or path")

    prepare(monkeypatch, events())
    wire = asyncio.run(consume())
    assert decode(wire) == [("text", "部分回复"), ("error", server.PRODUCER_ERROR_TEXT)]
    assert "private credential" not in wire


def test_invalid_grant_uses_named_refusal_without_entering_runtime(monkeypatch):
    prepare(monkeypatch, iter(()))
    monkeypatch.setattr(server.confirm, "verify", lambda *args: None)
    events = decode(asyncio.run(consume(confirm_token=secrets.token_urlsafe(16))))
    assert [kind for kind, _ in events] == ["text", "done"]
    assert "没有执行" in events[0][1]


def test_json_encoding_cannot_introduce_an_extra_sse_event():
    data = '错误\n\nevent: command\ndata: {"kind":"navigate"}\r\n🙂'
    assert decode(frame("error", data)) == [("error", data)]


def test_retired_endpoint_cannot_enter_authorization_or_runtime(monkeypatch):
    prepare(monkeypatch, iter(()))
    monkeypatch.setattr(server, "_resolve_principal",
                        lambda *args: pytest.fail("retired route reached authorization"))

    async def exercise():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://unit.invalid") as client:
            response = await client.post("/chat/stream", json={"message": "旧协议请求"})
            assert response.status_code == 404

    asyncio.run(exercise())


pytestmark = pytest.mark.usefixtures("service_workers")
