"""Actual streamed bytes extend API idle liveness, never its absolute deadline."""

import asyncio
import json
import threading
import time

import httpx
import pytest
from _service import app, service_request

from agent.api.routes import chat
from agent.api.schemas import ChatRequest
from agent.api.turns import next_stream_item
from agent.domains.image_generation import tools, waiting
from agent.principal import SOURCE_ASSERTION, Principal
from agent.runtime.results import TurnResult
from tools import base

ID = "6b9e2bf1-56af-428c-8177-e3ad0641e036"
pytestmark = pytest.mark.usefixtures("service_workers")


@pytest.mark.parametrize("case", ["active", "stalled", "total", "cancelled"])
def test_real_api_observes_io_activity_but_preserves_timeout_and_cancel(monkeypatch, case):
    closed, finished, calls, reasons = threading.Event(), threading.Event(), [], []
    value = {"id": ID, "requestHash": "a" * 64, "imageRemoved": False, "status": "generating",
             "request": {"provider": "https://images.test/v1", "prompt": "fixture",
                         "model": "fixture", "size": "1536x1024", "quality": "high",
                         "references": []}}

    def event(job):
        return ("event: image_job\ndata: " + json.dumps(job) + "\n\n").encode()

    class Body(httpx.SyncByteStream):
        def __iter__(self):
            yield event(value)
            if case == "stalled":
                threading.Event().wait(0.4)
            else:
                for _ in range(12):
                    threading.Event().wait(0.03)
                    yield b": actual upstream keepalive\n\n"
            yield event({**value, "status": "review", "outputHash": "b" * 64,
                "previewUrl": f"/api/public/image-generation/{ID}/preview?token=fixture"})

        def close(self):
            closed.set()

    def host(request):
        calls.append(request)
        return httpx.Response(200, headers={"Content-Type": "text/event-stream"}, stream=Body())

    class Runtime:
        def stream_events(self, state, config):
            if case == "total":
                # The absolute API deadline also applies to any other active upstream read.
                with base._client.stream("GET", "https://fixture.test/events") as response:
                    response.stream = waiting.CheckedStream(response.stream, config,
                                                             time.monotonic() + 10)
                    for _ in response.iter_bytes():
                        pass
                yield TurnResult("完成。")
            else:
                result = tools.get_image_generation.invoke({"job_id": ID}, config=config)
                yield TurnResult("核实完成。" if result.kind == "ok" else str(result))

    monkeypatch.setattr(app.state, "runtime", Runtime())
    monkeypatch.setattr(chat, "_resolve_principal",
                        lambda request, uid: Principal(7, "admin", SOURCE_ASSERTION))
    monkeypatch.setattr(chat, "build_messages", lambda *args, **kwargs: [])
    monkeypatch.setattr(chat, "start_trace", lambda *args, **kwargs: None)
    def finish(trace_id, reason, *args):
        reasons.append(reason)
        finished.set()

    monkeypatch.setattr(chat, "finish_trace", finish)
    monkeypatch.setattr(chat, "STREAM_IDLE_TIMEOUT", 0.15)
    monkeypatch.setattr(chat, "STREAM_TOTAL_TIMEOUT", 0.18 if case == "total" else 300)
    monkeypatch.setattr(chat, "STREAM_PING_INTERVAL", 0.04)

    async def poll(queue, producer):
        return await next_stream_item(queue, producer, timeout=0.02)

    monkeypatch.setattr(chat, "next_stream_item", poll)

    async def scenario():
        monkeypatch.setattr(app.state, "chat_slots", asyncio.BoundedSemaphore(1))
        response = await chat.chat_stream(ChatRequest(message="观察图片任务"), service_request())
        try:
            if case == "cancelled":
                first = await asyncio.wait_for(anext(response.body_iterator), 2)
                assert first.startswith("event: progress")
                await response.body_iterator.aclose()
            else:
                body = "".join([frame async for frame in response.body_iterator])
                if case == "active":
                    assert "event: done" in body and "event: error" not in body
                    assert body.count("event: progress") == 2, "keepalives are not UI progress"
                else:
                    assert "event: error" in body and "event: done" not in body
            assert await asyncio.to_thread(closed.wait, 2)
            assert await asyncio.to_thread(finished.wait, 2)
        finally:
            await response.body_iterator.aclose()
        assert len(calls) == 1 and calls[0].method == "GET"
        if case == "active":
            assert reasons == ["producer_done"]
        elif case == "stalled":
            assert reasons == ["idle_timeout"]
        elif case == "total":
            assert len(reasons) == 1 and reasons[0] in {"total_timeout", "producer_error"}

    with httpx.Client(transport=httpx.MockTransport(host)) as client:
        monkeypatch.setattr(base, "_client", client)
        asyncio.run(scenario())
