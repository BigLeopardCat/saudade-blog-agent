"""Request byte limits apply before handlers, independently of transport headers."""

import asyncio

import httpx
import pytest
from fastapi import Request
from starlette.responses import StreamingResponse

from agent.api import middleware
from agent.main import create_app


@pytest.mark.parametrize("declared", [None, "1", "16"])
def test_actual_bytes_reject_oversized_chunked_requests_without_entering_route(
        monkeypatch, declared):
    monkeypatch.setattr(middleware, "MAX_BODY_BYTES", 16)
    app = create_app()
    called = []

    @app.post("/body-probe")
    async def endpoint(request: Request):
        called.append(await request.body())
        return {"ok": True}

    async def run():
        async def body():
            yield b"a" * 8
            yield b"b" * 9

        headers = {} if declared is None else {"Content-Length": declared}
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app),
                                     base_url="http://test") as client:
            response = await client.post("/body-probe", content=body(), headers=headers)
        assert response.status_code == 413
        assert response.json() == {"error": "payload_too_large", "limit": 16}
        assert response.headers["X-Request-ID"]
        assert called == []

    asyncio.run(run())


def test_exact_byte_limit_preserves_utf8_body_and_streaming_response(monkeypatch):
    body = "猫咪喵".encode()
    monkeypatch.setattr(middleware, "MAX_BODY_BYTES", len(body))
    app = create_app()

    @app.post("/body-probe")
    async def endpoint(request: Request):
        received = await request.body()

        async def events():
            yield b"data: "
            yield received
            yield b"\n\n"

        return StreamingResponse(events(), media_type="text/event-stream")

    async def run():
        async def chunks():
            yield body[:4]
            yield body[4:]

        async with httpx.AsyncClient(transport=httpx.ASGITransport(app),
                                     base_url="http://test") as client:
            response = await client.post("/body-probe", content=chunks())
        assert response.status_code == 200
        assert response.content == b"data: " + body + b"\n\n"

    asyncio.run(run())


def test_declared_oversize_is_rejected_without_reading_body(monkeypatch):
    monkeypatch.setattr(middleware, "MAX_BODY_BYTES", 16)

    async def unexpected(*args):
        pytest.fail("Neither the body nor the handler should be read")

    async def run():
        sent = []

        async def send(message):
            sent.append(message)

        await middleware.BodyLimitMiddleware(unexpected)(
            {"type": "http", "headers": [(b"content-length", b"17")]}, unexpected, send)
        assert sent[0]["status"] == 413

    asyncio.run(run())


def test_disconnect_before_complete_body_never_enters_route(monkeypatch):
    monkeypatch.setattr(middleware, "MAX_BODY_BYTES", 16)

    async def unexpected(*args):
        pytest.fail("Disconnected partial requests must not enter a handler")

    async def run():
        messages = iter([{"type": "http.request", "body": b"part", "more_body": True},
                         {"type": "http.disconnect"}])

        async def receive():
            return next(messages)

        await middleware.BodyLimitMiddleware(unexpected)({"type": "http"}, receive, unexpected)

    asyncio.run(run())
