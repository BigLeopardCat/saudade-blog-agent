"""Actual service shutdown ordering with blocked workers and fake persistence."""

import asyncio
import secrets
import threading
import time
from types import SimpleNamespace

import httpx
import pytest

import server
from agent import checkpoint
from agent.runtime import confirmation
from agent.runtime.control import RequestControl
from agent.runtime.workers import RequestWorkers
from config.settings import settings


def control():
    return RequestControl(threading.Event(), time.monotonic() + 20)


@pytest.fixture
def startup(monkeypatch):
    events = []
    monkeypatch.setattr(settings, "llm_provider", "qwen")
    monkeypatch.setattr(server, "setup_logging", lambda: None)
    monkeypatch.setattr(server, "_build_arm", lambda: (object(), "native"))
    monkeypatch.setattr(server.wordgraph, "warm", lambda: events.append("wordgraph"))
    monkeypatch.setattr(server.rag_search, "warm", lambda: events.append("rag"))
    monkeypatch.setattr(checkpoint, "close_pool", lambda: events.append("pool_closed"))
    return events


@pytest.mark.parametrize("fail_at", ["provider", "runtime", "warmup_submission"])
def test_partial_startup_failure_closes_resources(startup, monkeypatch, fail_at):
    seen = []

    def fail():
        seen.append(server.app.state.workers)
        raise ValueError("startup fixture failed")

    if fail_at == "provider":
        monkeypatch.setattr(type(settings), "check_provider", lambda self: fail())
    elif fail_at == "runtime":
        monkeypatch.setattr(server, "_build_arm", fail)
    else:
        monkeypatch.setattr(RequestWorkers, "warm_up", lambda self, function: fail())

    async def run():
        with pytest.raises(ValueError, match="startup fixture failed"):
            async with server.lifespan(server.app):
                pytest.fail("failed startup yielded an application")

    asyncio.run(run())
    assert startup == ["pool_closed"]
    assert server._agent is None and server.app.state.workers is None
    with pytest.raises(RuntimeError, match="shutdown"):
        seen[0].executor.submit(lambda: None)


def test_lifespan_can_restart_with_fresh_executor_and_admission(startup):
    owners, permits = [], []

    async def run():
        for _ in range(2):
            async with server.lifespan(server.app):
                workers = server.app.state.workers
                owners.append(workers)
                permits.append(server._stream_slots)
                assert server._agent is not None
                assert await server._try_acquire_slot()
                server._release_slot()
            assert server._agent is None and server.app.state.workers is None
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
        lifecycle = server.lifespan(server.app)
        await lifecycle.__aenter__()
        workers = server.app.state.workers
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
            assert not await server._try_acquire_slot()
            readiness = await server.ready()
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

    monkeypatch.setattr(server.wordgraph, "warm", warm)

    async def run():
        lifecycle = server.lifespan(server.app)
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


@pytest.mark.parametrize("failure", ["construct", "setup"])
def test_saver_startup_failure_closes_pool_and_allows_retry(monkeypatch, failure):
    pools = []

    class Pool:
        closed = False

        def close(self):
            assert not self.closed
            self.closed = True

    def make_pool(dsn):
        pools.append(Pool())
        return pools[-1]

    def construct(pool):
        if failure == "construct":
            raise ValueError("constructor fixture")
        return SimpleNamespace(pool=pool)

    def setup(saver, *, pool):
        raise ValueError("setup fixture")

    monkeypatch.setattr(checkpoint, "_pool", None)
    monkeypatch.setattr(checkpoint, "_pool_dsn", None)
    monkeypatch.setattr(checkpoint, "_make_pool", make_pool)
    monkeypatch.setattr(checkpoint.importlib, "import_module",
                        lambda name: SimpleNamespace(PostgresSaver=construct))
    monkeypatch.setattr(checkpoint, "_setup_under_lock", setup)
    with pytest.raises(ValueError):
        checkpoint.build_checkpointer("postgresql://localhost/fixture")
    assert pools[0].closed and checkpoint._pool is None and checkpoint._pool_dsn is None
    monkeypatch.setattr(checkpoint.importlib, "import_module",
                        lambda name: SimpleNamespace(PostgresSaver=lambda pool: pool))
    monkeypatch.setattr(checkpoint, "_setup_under_lock", lambda *args, **kwargs: None)
    assert checkpoint.build_checkpointer("postgresql://localhost/fixture") is pools[1]
    assert not pools[1].closed
    checkpoint.close_pool()
    checkpoint.close_pool()
    assert pools[1].closed


def test_failed_pool_replacement_cannot_reuse_a_closed_pool(monkeypatch):
    closed = []
    old = SimpleNamespace(close=lambda: closed.append(True))
    monkeypatch.setattr(checkpoint, "_pool", old)
    monkeypatch.setattr(checkpoint, "_pool_dsn", "old")

    def fail(dsn):
        raise ValueError("pool fixture")

    monkeypatch.setattr(checkpoint, "_make_pool", fail)
    with pytest.raises(ValueError):
        checkpoint._pool_for("new")
    assert closed == [True] and checkpoint._pool is None and checkpoint._pool_dsn is None
    replacement = object()
    monkeypatch.setattr(checkpoint, "_make_pool", lambda dsn: replacement)
    assert checkpoint._pool_for("old") is replacement


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
        lifecycle = server.lifespan(server.app)
        await lifecycle.__aenter__()
        transport = httpx.ASGITransport(app=server.app)
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
