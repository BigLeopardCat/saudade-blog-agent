"""Actual service shutdown ordering with blocked workers and fake persistence."""


import asyncio
import secrets
import threading
import time

import httpx
import pytest
from _service import app, service_request

from agent import checkpoint
from agent.api import dependencies
from agent.api import lifecycle as app_lifecycle
from agent.api.routes import chat as server
from agent.api.routes import operations
from agent.products.blog import confirmation
from agent.runtime.control import RequestControl
from agent.runtime.workers import RequestWorkers
from config.settings import settings


def control():
    return RequestControl(threading.Event(), time.monotonic() + 20)


@pytest.fixture
def startup(monkeypatch):
    events = []
    monkeypatch.setattr(settings, "llm_provider", "qwen")
    monkeypatch.setattr(app_lifecycle, "setup_logging", lambda: None)
    def build(resources):
        resources.callback(events.append, "pool_closed")
        return object(), "native"
    monkeypatch.setattr(app_lifecycle, "build_runtime", build)
    monkeypatch.setattr(app_lifecycle.wordgraph, "warm", lambda: events.append("wordgraph"))
    monkeypatch.setattr(app_lifecycle.rag_search, "warm", lambda: events.append("rag"))
    return events


@pytest.mark.parametrize("fail_at", ["provider", "runtime", "warmup_submission"])
def test_partial_startup_failure_closes_resources(startup, monkeypatch, fail_at):
    seen = []

    def fail(*args):
        seen.append(app.state.workers)
        raise ValueError("startup fixture failed")

    if fail_at == "provider":
        monkeypatch.setattr(type(settings), "check_provider", lambda self: fail())
    elif fail_at == "runtime":
        monkeypatch.setattr(app_lifecycle, "build_runtime", fail)
    else:
        monkeypatch.setattr(RequestWorkers, "warm_up", lambda self, function: fail())

    async def run():
        with pytest.raises(ValueError, match="startup fixture failed"):
            async with app_lifecycle.lifespan(app):
                pytest.fail("failed startup yielded an application")

    asyncio.run(run())
    assert startup == (["pool_closed"] if fail_at == "warmup_submission" else [])
    assert app.state.runtime is None and app.state.workers is None
    with pytest.raises(RuntimeError, match="shutdown"):
        seen[0].executor.submit(lambda: None)


def test_lifespan_can_restart_with_fresh_executor_and_admission(startup):
    owners, permits = [], []

    async def run():
        for _ in range(2):
            async with app_lifecycle.lifespan(app):
                workers = app.state.workers
                owners.append(workers)
                permits.append(app.state.chat_slots)
                assert app.state.runtime is not None
                assert await dependencies.try_acquire_slot(service_request())
                dependencies.release_slot(service_request())
            assert app.state.runtime is None and app.state.workers is None
            with pytest.raises(RuntimeError, match="shutdown"):
                workers.executor.submit(lambda: None)

    asyncio.run(run())
    assert owners[0] is not owners[1] and permits[0] is not permits[1]
    assert startup.count("pool_closed") == 2


def test_shutdown_drains_workers_and_trace_before_closing_pool(startup, monkeypatch):
    entered, unblock = threading.Event(), threading.Event()

    def producer(loop):
        entered.set()
        assert unblock.wait(5)
        # Shutdown must keep the event loop alive for thread-to-loop delivery.
        future = asyncio.run_coroutine_threadsafe(asyncio.sleep(0), loop)
        future.result(timeout=2)
        startup.append("worker_exit")

    async def run():
        lifecycle = app_lifecycle.lifespan(app)
        await lifecycle.__aenter__()
        workers = app.state.workers
        loop = asyncio.get_running_loop()
        jobs = workers.request(loop, lambda: startup.append("released"), control=control(),
                               on_complete=lambda: startup.append("trace_closed"))
        jobs.submit(workers.executor, producer, loop)
        assert await asyncio.to_thread(entered.wait, 2)
        ending = asyncio.create_task(lifecycle.__aexit__(None, None, None))
        try:
            await asyncio.sleep(.02)
            assert workers.closing and jobs.control.stop_event.is_set()
            assert not ending.done() and "pool_closed" not in startup
            assert not await dependencies.try_acquire_slot(service_request())
            readiness = await operations.ready(service_request())
            assert readiness.status_code == 503 and b"shutting_down" in readiness.body
            with pytest.raises(RuntimeError, match="shutting down"):
                workers.request(loop, lambda: None, control=control())
        finally:
            unblock.set()
            await asyncio.wait_for(ending, 3)
        assert startup.index("worker_exit") < startup.index("trace_closed")
        assert startup.index("trace_closed") < startup.index("released")
        assert startup.index("released") < startup.index("pool_closed")
        await jobs.wait_closed()

    asyncio.run(run())


def test_queued_work_is_cancelled_and_shutdown_is_idempotent():
    entered, unblock = threading.Event(), threading.Event()
    calls = []

    async def run():
        workers = RequestWorkers(max_workers=1)
        jobs = workers.request(asyncio.get_running_loop(), lambda: calls.append("released"),
                               control=control())

        def hold():
            entered.set()
            assert unblock.wait(5)

        first = jobs.submit(workers.executor, hold)
        assert await asyncio.to_thread(entered.wait, 2)
        queued = jobs.submit(workers.executor, lambda: calls.append("unwanted"))
        ending = asyncio.create_task(workers.aclose())
        try:
            await asyncio.sleep(.02)
            assert not ending.done() and not first.done()
        finally:
            unblock.set()
            await asyncio.wait_for(ending, 3)
        with pytest.raises(asyncio.CancelledError):
            await queued
        await workers.aclose()
        assert calls == ["released"]

    asyncio.run(run())


def test_trace_finalization_failure_does_not_leak_admission_or_block_shutdown():
    released, errors = [], []

    def failed_trace():
        raise ValueError("trace finalization fixture")

    async def run():
        loop = asyncio.get_running_loop()
        loop.set_exception_handler(lambda loop, context: errors.append(context["exception"]))
        workers = RequestWorkers()
        jobs = workers.request(loop, lambda: released.append(True), control=control(),
                               on_complete=failed_trace)
        await jobs.submit(workers.executor, lambda: None)
        await asyncio.wait_for(workers.aclose(), 2)
        await jobs.wait_closed()

    asyncio.run(run())
    assert released == [True]
    assert len(errors) == 1 and str(errors[0]) == "trace finalization fixture"


@pytest.mark.parametrize("cancellations", [1, 3])
def test_cancelled_shutdown_waits_for_its_threads_before_propagating(cancellations):
    entered, unblock = threading.Event(), threading.Event()

    async def run():
        workers = RequestWorkers(max_workers=1)
        jobs = workers.request(asyncio.get_running_loop(), lambda: None, control=control())

        def hold():
            entered.set()
            assert unblock.wait(5)

        jobs.submit(workers.executor, hold)
        assert await asyncio.to_thread(entered.wait, 2)
        ending = asyncio.create_task(workers.aclose())
        try:
            await asyncio.sleep(.02)
            for _ in range(cancellations):
                ending.cancel()
                await asyncio.sleep(.02)
                assert not ending.done()
        finally:
            unblock.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(ending, 3)
        await jobs.wait_closed()
        assert not workers._requests

    asyncio.run(run())


def test_optional_warmups_are_joined_and_failures_are_logged(startup, monkeypatch, caplog):
    entered, unblock = threading.Event(), threading.Event()

    def warm():
        entered.set()
        assert unblock.wait(5)
        raise ValueError("optional warmup fixture")

    monkeypatch.setattr(app_lifecycle.wordgraph, "warm", warm)

    async def run():
        lifecycle = app_lifecycle.lifespan(app)
        await lifecycle.__aenter__()
        assert await asyncio.to_thread(entered.wait, 2)
        ending = asyncio.create_task(lifecycle.__aexit__(None, None, None))
        try:
            await asyncio.sleep(.02)
            assert not ending.done() and "pool_closed" not in startup
        finally:
            unblock.set()
            await asyncio.wait_for(ending, 3)

    asyncio.run(run())
    assert "optional warmup fixture" in caplog.text
    assert startup[-1] == "pool_closed"


@pytest.mark.parametrize("method,path,body,target", [
    ("DELETE", "/checkpoint/7", {}, "delete"),
    ("POST", "/checkpoint/7/drop_turn", {"text": "discarded"}, "discard"),
    ("POST", "/confirmation/cancel", {"user_id": 7, "conversation_id": 7}, "cancel"),
])
def test_disconnected_maintenance_operations_drain_before_pool_close(
        startup, monkeypatch, method, path, body, target):
    entered, unblock = threading.Event(), threading.Event()
    monkeypatch.setattr(settings, "agent_require_assertion", False)
    monkeypatch.setattr(checkpoint, "live_saver", lambda runtime: object())
    monkeypatch.setattr(server.confirm, "verify", lambda *args: {"jti": "fixture"})

    def operation(*args):
        entered.set()
        assert unblock.wait(5)
        startup.append("maintenance_exit")
        return {"dropped": True, "cancelled": True}

    if target == "delete":
        monkeypatch.setattr(checkpoint, "delete_thread", operation)
    elif target == "discard":
        monkeypatch.setattr(checkpoint, "drop_last_user_turn", operation)
    else:
        monkeypatch.setattr(confirmation, "cancel", operation)

    async def run():
        lifecycle = app_lifecycle.lifespan(app)
        await lifecycle.__aenter__()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            payload = {**body, "token": secrets.token_urlsafe()}
            pending = asyncio.create_task(client.request(method, path, json=payload))
            assert await asyncio.to_thread(entered.wait, 2)
            pending.cancel()
            with pytest.raises(asyncio.CancelledError):
                await pending
        ending = asyncio.create_task(lifecycle.__aexit__(None, None, None))
        try:
            await asyncio.sleep(.02)
            assert not ending.done() and "pool_closed" not in startup
        finally:
            unblock.set()
            await asyncio.wait_for(ending, 3)
        assert startup.index("maintenance_exit") < startup.index("pool_closed")

    asyncio.run(run())
