"""Cleanup deadlines cannot report live resources as released or reuse a broken pool."""

import asyncio
import secrets
import threading
import time
from types import SimpleNamespace

import anyio
import httpx
import pytest
from _service import app, service_request

from agent.api.routes.operations import health
from agent.runtime_probe import ready_payload
from models.transport import ModelCleanupError, ModelIO, io_scope


class Stream:
    def __init__(self, release=None):
        self.release = release
        self.closed = threading.Event()

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not getattr(self, "started", False):
            self.started = True
            return "first"
        await anyio.sleep_forever()

    async def aclose(self):
        if self.release is None:
            await anyio.sleep_forever()
        else:
            with anyio.CancelScope(shield=True):
                while not self.release.is_set():
                    await anyio.sleep(0.001)
        self.closed.set()


def wait_for_owned_cleanup(io):
    end = time.monotonic() + 2
    while io.pending_cleanup and time.monotonic() < end:
        time.sleep(0.005)
    assert io.pending_cleanup == 0


def test_cooperative_close_is_bounded_and_failed_pool_cannot_be_reused(monkeypatch):
    records = []
    monkeypatch.setattr("models.transport.record", lambda *a, **kw: records.append((a, kw)))
    resource = Stream()
    with ModelIO(cleanup_timeout=0.03) as io, io_scope(io):
        iterator = io.iterate(lambda: resource)
        assert next(iterator) == "first"
        assert io.ready and io.pending_cleanup == 0
        started = time.monotonic()
        iterator.close()
        assert time.monotonic() - started < 1
        assert not io.ready and not resource.closed.is_set()
        assert io.pending_cleanup == 0
        with pytest.raises(ModelCleanupError):
            io.client
        closed = [fields for args, fields in records if args == ("model", "transport_closed")]
        assert closed and closed[-1]["producer_exited"]
        assert closed[-1]["cleanup_complete"] is False


def test_shielded_cleanup_retains_ownership_and_fails_readiness_until_actual_exit(monkeypatch):
    release = threading.Event()
    resource = Stream(release)
    io = ModelIO(cleanup_timeout=0.03)
    try:
        with io_scope(io):
            iterator = io.iterate(lambda: resource)
            assert next(iterator) == "first"
            assert io.ready and io.pending_cleanup == 0
            started = time.monotonic()
            iterator.close()
            assert time.monotonic() - started < 1
            assert io.pending_cleanup == 1 and not io.ready
            with pytest.raises(ModelCleanupError):
                next(io.iterate(lambda: resource))
            settings = SimpleNamespace(check_provider=lambda: "opencode",
                                       is_api_key_configured=True,
                                       agent_assertion_secret=secrets.token_urlsafe(32),
                                       confirmation_secret=secrets.token_urlsafe(32))
            runtime = SimpleNamespace(model_io=io)
            monkeypatch.setattr(app.state, "runtime", runtime)
            diagnostics = asyncio.run(health(service_request()))
            assert diagnostics["model_transport"] == {"ready": False, "pending_cleanup": 1}
            code, body = ready_payload(runtime, settings)
            assert code == 503 and "model_transport_unavailable" in body["reasons"]
            with pytest.raises(ModelCleanupError, match="still own resources"):
                io.close()
    finally:
        release.set()
        assert resource.closed.wait(2)
        wait_for_owned_cleanup(io)
        io.close()


def test_pool_shutdown_has_its_own_deadline_and_keeps_pending_cleanup_visible():
    release, exited = threading.Event(), threading.Event()

    class Transport(httpx.AsyncBaseTransport):
        async def aclose(self):
            with anyio.CancelScope(shield=True):
                while not release.is_set():
                    await anyio.sleep(0.001)
            exited.set()

    io = ModelIO(transport=Transport(), cleanup_timeout=0.03)
    io.client
    try:
        started = time.monotonic()
        with pytest.raises(ModelCleanupError, match="still owns resources"):
            io.close()
        assert time.monotonic() - started < 1
        assert io.pending_cleanup == 1 and not io.ready
    finally:
        release.set()
        assert exited.wait(2)
        wait_for_owned_cleanup(io)
        # The deadline remains a reported failure even after actual late cleanup.
        with pytest.raises(ModelCleanupError, match="deadline exceeded"):
            io.close()
