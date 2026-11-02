"""An ASGI send failure must close ownership even before the body starts."""

import asyncio
import threading
import time

import anyio
import pytest
from _service import app, service_request
from langchain_core.messages import AIMessageChunk
from starlette.requests import ClientDisconnect

from agent.api import events
from agent.api.routes import chat
from agent.api.schemas import ChatRequest
from agent.principal import SOURCE_ASSERTION, Principal
from agent.runtime.results import TurnResult


@pytest.fixture
def endpoint(monkeypatch):
    calls, closed = [], threading.Event()

    class Runtime:
        def stream_events(self, state, config):
            calls.append(True)
            try:
                for _ in range(1000):
                    yield AIMessageChunk(content="x")
                yield TurnResult("x" * 1000)
            finally:
                closed.set()

    monkeypatch.setattr(app.state, "runtime", Runtime())
    monkeypatch.setattr(chat, "_resolve_principal",
                        lambda request, uid: Principal(7, "user", SOURCE_ASSERTION))
    monkeypatch.setattr(chat, "build_messages", lambda *args, **kwargs: [])
    monkeypatch.setattr(chat, "start_trace", lambda *args, **kwargs: None)
    monkeypatch.setattr(chat, "finish_trace", lambda *args, **kwargs: None)
    monkeypatch.setattr(events, "SEND_TIMEOUT", 0.03)
    return calls, closed


@pytest.mark.parametrize("phase", ["headers", "body"])
@pytest.mark.parametrize("failure", ["timeout", "disconnect"])
@pytest.mark.parametrize("spec", ["2.3", "2.4"])
def test_send_failure_closes_generator_and_releases_admission(
    endpoint, phase, failure, spec, monkeypatch
):
    calls, closed = endpoint

    async def scenario():
        slots = asyncio.BoundedSemaphore(1)
        monkeypatch.setattr(app.state, "chat_slots", slots)
        response = await chat.chat_stream(ChatRequest(message="fixture"), service_request())
        assert slots.locked()

        async def send(message):
            if message["type"] == ("http.response.start" if phase == "headers"
                                   else "http.response.body"):
                if failure == "disconnect":
                    raise OSError("fixture client disconnected")
                await anyio.sleep_forever()

        async def receive():
            await anyio.sleep_forever()

        started = time.monotonic()
        with pytest.raises((ClientDisconnect, OSError)):
            await response({"type": "http", "asgi": {"spec_version": spec}}, receive, send)
        deadline = time.monotonic() + 1
        while slots.locked() and time.monotonic() < deadline:
            await asyncio.sleep(0.005)
        assert not slots.locked(), "failed send leaked its admission permit"
        assert time.monotonic() - started < 1
        assert len(calls) == (1 if phase == "body" else 0)
        assert closed.is_set() == (phase == "body")
        await slots.acquire()
        slots.release()

    asyncio.run(scenario())


pytestmark = pytest.mark.usefixtures("service_workers")
