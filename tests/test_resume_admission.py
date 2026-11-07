"""Explicit continuation waits for real cleanup without freeing running permits."""

import asyncio
import threading
import time

import pytest

from agent.runtime.control import RequestControl
from agent.runtime.workers import RequestWorkers


def test_continuation_waits_for_cleanup_and_does_not_wait_for_other_sessions():
    async def scenario():
        workers = RequestWorkers(max_workers=1)
        entered, unblock = threading.Event(), threading.Event()
        released = []
        jobs = workers.request(asyncio.get_running_loop(), lambda: released.append(True),
                               control=RequestControl(threading.Event(), time.monotonic() + 10,
                                                      session_id="owner-7-conversation-9"))

        def blocked():
            entered.set()
            assert unblock.wait(3)

        jobs.submit(workers.executor, blocked)
        try:
            assert await asyncio.to_thread(entered.wait, 2)
            jobs.close()
            assert await workers.wait_for_session("other-owner", timeout=0.01)
            continuation = asyncio.create_task(workers.wait_for_session("owner-7-conversation-9"))
            await asyncio.sleep(0)
            assert not continuation.done() and not released
            unblock.set()
            assert await asyncio.wait_for(continuation, 2)
            assert released == [True]
        finally:
            unblock.set()
            await workers.aclose()

    asyncio.run(scenario())


@pytest.mark.usefixtures("service_workers")
def test_stream_resume_waits_for_the_previous_producer_before_admission(monkeypatch):
    from _service import app, service_request
    from langchain_core.messages import AIMessageChunk

    from agent.api.routes import chat as server
    from agent.api.schemas import ChatRequest
    from agent.principal import SOURCE_ASSERTION, Principal

    entered, unblock = threading.Event(), threading.Event()
    calls = []
    monkeypatch.setattr(app.state, "runtime", object())
    monkeypatch.setattr(app.state, "chat_slots", asyncio.BoundedSemaphore(1))
    monkeypatch.setattr(server, "_resolve_principal",
                        lambda request, uid: Principal(7, "user", SOURCE_ASSERTION))
    monkeypatch.setattr(server, "start_trace", lambda *args, **kwargs: None)
    monkeypatch.setattr(server, "finish_trace", lambda *args, **kwargs: None)

    def producer(runtime, messages, thread, queue, loop, uid, stop, *args, **kwargs):
        calls.append(kwargs.get("resume_task"))
        entered.set()
        asyncio.run_coroutine_threadsafe(queue.put(AIMessageChunk(content="hello")), loop).result(2)
        assert unblock.wait(3)

    monkeypatch.setattr(server, "stream_to_queue", producer)

    async def scenario():
        first = await server.chat_stream(ChatRequest(message="hello", conversation_id=9),
                                         service_request())
        resumed = None
        try:
            assert "hello" in await asyncio.wait_for(anext(first.body_iterator), 2)
            assert entered.is_set()
            await first.body_iterator.aclose()
            waiting = asyncio.create_task(server.chat_stream(ChatRequest(
                message="", conversation_id=9, resume_task="same-task"), service_request()))
            await asyncio.sleep(0)
            assert not waiting.done() and len(calls) == 1
            unblock.set()
            resumed = await asyncio.wait_for(waiting, 2)
            assert "hello" in await asyncio.wait_for(anext(resumed.body_iterator), 2)
            assert calls == [None, "same-task"]
        finally:
            unblock.set()
            if resumed is not None:
                await resumed.body_iterator.aclose()

    asyncio.run(scenario())


@pytest.mark.parametrize("cancel", [False, True])
def test_wait_timeout_or_disconnect_keeps_the_actual_worker_and_permit(cancel):
    async def scenario():
        workers = RequestWorkers(max_workers=1)
        entered, unblock = threading.Event(), threading.Event()
        releases = []
        jobs = workers.request(asyncio.get_running_loop(), lambda: releases.append(True),
                               control=RequestControl(threading.Event(), time.monotonic() + 10,
                                                      session_id="owner-7-conversation-9"))

        def blocked():
            entered.set()
            assert unblock.wait(3)

        future = jobs.submit(workers.executor, blocked)
        try:
            assert await asyncio.to_thread(entered.wait, 2)
            jobs.close()
            waiting = asyncio.create_task(workers.wait_for_session(
                "owner-7-conversation-9", timeout=0.01 if not cancel else 5))
            if cancel:
                await asyncio.sleep(0)
                waiting.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await waiting
            else:
                assert not await waiting
            assert not future.done() and not releases
            unblock.set()
            await asyncio.wait_for(jobs.wait_closed(), 2)
            assert releases == [True]
        finally:
            unblock.set()
            await workers.aclose()

    asyncio.run(scenario())
