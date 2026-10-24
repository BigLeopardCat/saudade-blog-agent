"""Exercise package entry, request-owned resources and independent application instances."""

import asyncio
import os
import secrets
import subprocess
import sys
from pathlib import Path

import httpx
import pytest
from langchain_core.messages import AIMessageChunk

from agent.api import dependencies, middleware
from agent.api.routes import chat
from agent.main import app, create_app
from agent.runtime.results import TurnResult
from agent.runtime.workers import RequestWorkers
from config.settings import settings


class Runtime:
    def __init__(self, name):
        self.name, self.calls = name, []

    def stream_events(self, state, config):
        self.calls.append((state, config))
        yield AIMessageChunk(content=self.name)
        yield TurnResult(self.name)


@pytest.fixture
def identity(monkeypatch):
    monkeypatch.setattr(settings, "agent_require_assertion", False)


def test_package_entry_imports_outside_checkout_without_starting_resources(tmp_path):
    root = Path(__file__).resolve().parents[1]
    script = """
from agent.main import app, create_app
import server
assert server.app is app
assert app.state.runtime is None and app.state.workers is None
assert create_app() is not app
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=tmp_path,
        timeout=20,
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONPATH": str(root)},
    )
    assert result.returncode == 0, result.stderr


def test_routers_import_without_loading_application_entry(tmp_path):
    root = Path(__file__).resolve().parents[1]
    script = """
import sys
from agent.api.routes import chat, conversations, operations
assert 'agent.main' not in sys.modules and 'server' not in sys.modules
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=tmp_path,
        timeout=20,
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONPATH": str(root)},
    )
    assert result.returncode == 0, result.stderr


def test_concurrent_applications_use_their_own_runtime_and_admission(identity):
    async def run():
        first, second = create_app(), create_app()
        for application, name in ((first, "first"), (second, "second")):
            application.state.runtime = Runtime(name)
            application.state.workers = RequestWorkers()
            application.state.chat_slots = asyncio.BoundedSemaphore(1)
        try:
            async with (
                httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=first), base_url="http://first"
                ) as one,
                httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=second), base_url="http://second"
                ) as two,
            ):
                replies = await asyncio.gather(
                    one.post("/chat", json={"message": "one", "user_id": 1}),
                    two.post("/chat", json={"message": "two", "user_id": 2}),
                )
                assert [reply.json()["reply"] for reply in replies] == ["first", "second"]
                first.state.workers.stop()
                assert (await one.post("/chat", json={"message": "stopped"})).status_code == 503
                assert (await two.post("/chat", json={"message": "still alive"})).status_code == 200
                assert len(first.state.runtime.calls) == 1
                assert len(second.state.runtime.calls) == 2
                assert app.state.runtime is None
        finally:
            await first.state.workers.aclose()
            await second.state.workers.aclose()

    asyncio.run(run())


def test_saturated_application_does_not_consume_another_applications_permit(identity, monkeypatch):
    monkeypatch.setattr(dependencies, "STREAM_QUEUE_WAIT", 0.01)

    async def run():
        first, second = create_app(), create_app()
        for application in (first, second):
            application.state.runtime = Runtime("ok")
            application.state.workers = RequestWorkers()
            application.state.chat_slots = asyncio.BoundedSemaphore(1)
        await first.state.chat_slots.acquire()
        try:
            async with (
                httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=first), base_url="http://first"
                ) as one,
                httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=second), base_url="http://second"
                ) as two,
            ):
                assert (await one.post("/chat", json={"message": "busy"})).status_code == 503
                assert (await two.post("/chat", json={"message": "free"})).json()["reply"] == "ok"
                assert not first.state.runtime.calls
        finally:
            first.state.chat_slots.release()
            await first.state.workers.aclose()
            await second.state.workers.aclose()

    asyncio.run(run())


def test_mounted_summary_and_body_limits_use_the_new_routes(identity, monkeypatch):
    seen = []
    monkeypatch.setattr(
        chat,
        "summarize",
        lambda message, history, old: (seen.append((message, history, old)) or "new summary"),
    )

    async def run():
        application = create_app()
        application.state.runtime = Runtime("reply")
        application.state.workers = RequestWorkers()
        application.state.chat_slots = asyncio.BoundedSemaphore(1)
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=application), base_url="http://test"
            ) as client:
                reply = await client.post(
                    "/chat",
                    json={"message": "remember", "summary": "old summary", "needs_summary": True},
                )
                assert reply.json()["new_summary"] == "new summary"
                assert seen == [("remember", [], "old summary")]
                monkeypatch.setattr(middleware, "MAX_BODY_BYTES", 20)
                assert (await client.post("/chat", json={"message": "x" * 40})).status_code == 413
                assert len(application.state.runtime.calls) == 1
        finally:
            await application.state.workers.aclose()

    asyncio.run(run())


def test_inconsistent_confirmation_narrowing_refuses_without_entering_runtime(
    identity, monkeypatch
):
    monkeypatch.setattr(chat.confirm, "verify", lambda *args: {"specs": []})
    monkeypatch.setattr(chat.confirm, "narrow", lambda *args: (None, ""))

    async def run():
        application = create_app()
        application.state.runtime = Runtime("must not run")
        application.state.workers = RequestWorkers()
        application.state.chat_slots = asyncio.BoundedSemaphore(1)
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=application), base_url="http://test"
            ) as client:
                response = await client.post(
                    "/chat/stream/v2",
                    json={
                        "message": "",
                        "confirm_token": secrets.token_urlsafe(),
                        "confirm_pick": "1",
                    },
                )
                assert response.status_code == 200
                assert chat.INVALID_PICK_TEXT in response.text
                assert not application.state.runtime.calls
        finally:
            await application.state.workers.aclose()

    asyncio.run(run())
