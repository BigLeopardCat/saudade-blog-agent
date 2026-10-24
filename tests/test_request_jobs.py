"""Admission follows worker completion, including cancellation and queued jobs."""


import asyncio
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from _service import app, service_request
from fastapi import HTTPException

from agent.api import dependencies
from agent.api.schemas import ChatRequest
from agent.principal import SOURCE_ASSERTION, Principal
from agent.runtime.control import (
    AgentCancelled,
    RequestControl,
    check_active,
    model_options,
    request_scope,
)
from agent.runtime.jobs import RequestJobs


def test_cancelled_running_future_retains_permit_until_worker_exits():
    entered, unblock = threading.Event(), threading.Event()

    async def scenario(executor):
        released = asyncio.Event()
        releases = []
        jobs = RequestJobs(asyncio.get_running_loop(),
                           lambda: (releases.append(True), released.set()),
                           control=RequestControl(threading.Event(), time.monotonic() + 10))

        def blocked():
            entered.set()
            assert unblock.wait(3)

        future = jobs.submit(executor, blocked)
        try:
            assert await asyncio.to_thread(entered.wait, 2)
            future.cancel()
            jobs.close()
            jobs.close()
            await asyncio.sleep(0)
            assert jobs.control.stop_event.is_set() and not releases
        finally:
            unblock.set()
        await asyncio.wait_for(released.wait(), 2)
        assert releases == [True]

    with ThreadPoolExecutor(max_workers=1) as executor:
        asyncio.run(scenario(executor))


def test_queued_job_cancels_without_execution_and_releases_once():
    unblock = threading.Event()
    with ThreadPoolExecutor(max_workers=1) as executor:
        blocker = executor.submit(unblock.wait, 3)

        async def scenario():
            released, invoked = asyncio.Event(), []
            jobs = RequestJobs(asyncio.get_running_loop(), released.set,
                               control=RequestControl(threading.Event(), time.monotonic() + 10))
            future = jobs.submit(executor, lambda: invoked.append(True))
            jobs.close()
            await asyncio.wait_for(released.wait(), 2)
            with pytest.raises(asyncio.CancelledError):
                await future
            assert not invoked and not blocker.done()

        try:
            asyncio.run(scenario())
        finally:
            unblock.set()


def test_group_waits_for_summary_and_producer_and_recovers_submit_failure():
    async def scenario(executor):
        released, calls = asyncio.Event(), []
        jobs = RequestJobs(asyncio.get_running_loop(), released.set,
                           control=RequestControl(threading.Event(), time.monotonic() + 10))
        assert await jobs.submit(executor, lambda: "summary") == "summary"
        assert await jobs.submit(executor, lambda: "reply") == "reply"
        assert not released.is_set()
        jobs.close()
        await asyncio.wait_for(released.wait(), 2)
        with pytest.raises(RuntimeError, match="closed"):
            jobs.submit(executor, lambda: calls.append(True))
        assert not calls
        failed = RequestJobs(asyncio.get_running_loop(), lambda: calls.append("released"),
                             control=RequestControl(threading.Event(), time.monotonic() + 10))
        executor.shutdown(wait=False)
        with pytest.raises(RuntimeError):
            failed.submit(executor, lambda: None)
        failed.close()
        await asyncio.sleep(0)
        assert calls == ["released"]

    with ThreadPoolExecutor(max_workers=1) as executor:
        asyncio.run(scenario(executor))


def test_deadline_and_stop_are_shared_across_worker_jobs_and_scope_resets(monkeypatch):
    async def scenario(executor):
        control = RequestControl(threading.Event(), 110.0)
        jobs = RequestJobs(asyncio.get_running_loop(), lambda: None, control=control)
        monkeypatch.setattr("agent.runtime.control.time.monotonic", lambda: 100.0)
        assert await jobs.submit(executor, lambda: model_options(120)) == {"timeout": 10.0}
        monkeypatch.setattr("agent.runtime.control.time.monotonic", lambda: 107.0)
        assert await jobs.submit(executor, lambda: model_options(120)) == {"timeout": 3.0}
        control.stop_event.set()
        with pytest.raises(AgentCancelled):
            await jobs.submit(executor, lambda: pytest.fail("cancelled work ran"))
        assert model_options(120) == {}
        jobs.close()
        await asyncio.sleep(0)

    # Patching the shared time module would also freeze asyncio's clock.
    from types import SimpleNamespace

    monkeypatch.setattr("agent.runtime.control.time", SimpleNamespace(monotonic=lambda: 100.0))
    with ThreadPoolExecutor(max_workers=1) as executor:
        asyncio.run(scenario(executor))
    with request_scope(RequestControl(threading.Event(), 108.0)):
        monkeypatch.setattr("agent.runtime.control.time.monotonic", lambda: 109.0)
        with pytest.raises(TimeoutError):
            check_active()
    assert model_options(120) == {}


def test_sse_close_keeps_admission_and_trace_until_real_producer_exit(monkeypatch):
    from agent.api.routes import chat as server

    entered, unblock, stopped = threading.Event(), threading.Event(), []
    finalized = []
    monkeypatch.setattr(app.state, "runtime", object())
    monkeypatch.setattr(server, "_resolve_principal",
                        lambda request, uid: Principal(7, "user", SOURCE_ASSERTION))
    monkeypatch.setattr(app.state, "chat_slots", asyncio.BoundedSemaphore(1))
    monkeypatch.setattr(dependencies, "STREAM_QUEUE_WAIT", .01)
    monkeypatch.setattr(server, "start_trace", lambda *args, **kwargs: None)
    monkeypatch.setattr(server, "finish_trace", lambda *args: finalized.append(args))

    def producer(runtime, messages, thread, queue, loop, uid, stop, *args):
        entered.set()
        from langchain_core.messages import AIMessageChunk

        asyncio.run_coroutine_threadsafe(queue.put(AIMessageChunk(content="hello")), loop).result(2)
        assert unblock.wait(3)
        stopped.append(stop.is_set())

    monkeypatch.setattr(server, "stream_to_queue", producer)

    async def scenario():
        drained = asyncio.Event()
        monkeypatch.setattr(server, "finish_trace",
                            lambda *args: (finalized.append(args), drained.set()))
        response = await server.chat_stream(ChatRequest(message="hello"), service_request())
        try:
            assert "hello" in await asyncio.wait_for(anext(response.body_iterator), 2)
            assert entered.is_set()
            await response.body_iterator.aclose()
            assert not finalized and not await dependencies.try_acquire_slot(service_request())
            # Nonstreaming requests share admission with detached streaming work.
            with pytest.raises(HTTPException) as refused:
                await server.chat(ChatRequest(message="new"), service_request())
            assert refused.value.status_code == 503
        finally:
            unblock.set()
        await asyncio.wait_for(drained.wait(), 2)
        assert await dependencies.try_acquire_slot(service_request())
        dependencies.release_slot(service_request())
        assert len(finalized) == 1 and stopped == [True]

    asyncio.run(scenario())


def test_model_factory_uses_remaining_sdk_timeout_without_transport_retries(monkeypatch):
    from types import SimpleNamespace

    from models import llm

    monkeypatch.setattr("agent.runtime.control.time", SimpleNamespace(monotonic=lambda: 100.0))
    monkeypatch.setattr(llm, "ChatOpenAI", lambda **kwargs: kwargs)
    with request_scope(RequestControl(threading.Event(), 108.0)):
        options = llm.get_llm(timeout=120, api_key="test-only", base_url="https://example.test")
    assert options["timeout"] == 8.0 and options["max_retries"] == 0
    normal = llm.get_llm(timeout=120, api_key="test-only", base_url="https://example.test")
    assert normal["timeout"] == 120 and "max_retries" not in normal


pytestmark = pytest.mark.usefixtures("service_workers")
