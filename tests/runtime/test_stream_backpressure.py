"""Slow readers cannot grow buffers, hide failures or prevent cancellation."""

import asyncio
import threading
import time

import pytest
from langchain_core.messages import AIMessageChunk

from agent.api.turns import ProducerCancelled, next_stream_item, stream_to_queue
from agent.runtime.control import RequestControl
from agent.runtime.progress import StreamProgress
from agent.runtime.results import TurnResult


@pytest.mark.parametrize("failure", [False, True])
def test_slow_consumer_preserves_order_and_observes_terminal_result(failure):
    class Runtime:
        def stream_events(self, state, config):
            for _ in range(80):
                yield AIMessageChunk(content="x")
            if failure:
                raise OSError("fixture storage failed")
            yield TurnResult("x" * 80)

    async def scenario():
        queue = asyncio.Queue(maxsize=2)
        task = asyncio.create_task(asyncio.to_thread(
            stream_to_queue, Runtime(), [], "fixture", queue, asyncio.get_running_loop()))
        parts = []
        while True:
            item = await next_stream_item(queue, task)
            assert queue.qsize() <= 2
            if isinstance(item, AIMessageChunk):
                parts.append(item.content)
                await asyncio.sleep(0.001)
            else:
                assert isinstance(item, OSError) if failure else item is None
                break
        await task
        assert "".join(parts) == "x" * 80
        assert queue.empty()

    asyncio.run(scenario())


def test_cancel_full_buffer_exits_without_waiting_for_reader_and_closes_generator():
    produced, closed, stop = [], threading.Event(), threading.Event()

    class Runtime:
        def stream_events(self, state, config):
            try:
                for _ in range(10000):
                    produced.append(True)
                    yield AIMessageChunk(content="x")
                yield TurnResult("x" * 10000)
            finally:
                closed.set()

    async def scenario():
        queue = asyncio.Queue(maxsize=2)
        task = asyncio.create_task(asyncio.to_thread(
            stream_to_queue, Runtime(), [], "fixture", queue, asyncio.get_running_loop(),
            stop_event=stop))
        deadline = time.monotonic() + 2
        while len(produced) < 3 and time.monotonic() < deadline:
            await asyncio.sleep(0.005)
        assert len(produced) == 3 and queue.full()
        await asyncio.sleep(0.1)
        assert len(produced) == 3, "producer ran past the bounded buffer"
        started = time.monotonic()
        stop.set()
        result = await asyncio.wait_for(task, 1)
        assert isinstance(result, ProducerCancelled)
        assert time.monotonic() - started < 1 and closed.is_set()
        # Termination is outside the full buffer; queued text remains ordered.
        assert queue.qsize() == 2
        for _ in range(2):
            assert isinstance(await next_stream_item(queue, task), AIMessageChunk)
        assert isinstance(await next_stream_item(queue, task), ProducerCancelled)
        await queue.put("next")
        assert await queue.get() == "next", "cancelled getter stole a later frame"

    asyncio.run(scenario())


def test_progress_saturation_does_not_overwrite_text_or_raise_in_event_loop():
    async def scenario():
        loop, errors = asyncio.get_running_loop(), []
        previous = loop.get_exception_handler()
        loop.set_exception_handler(lambda _, context: errors.append(context))
        try:
            queue = asyncio.Queue(maxsize=1)
            await queue.put("body")
            progress = StreamProgress(loop, queue, RequestControl(
                threading.Event(), time.monotonic() + 10))
            assert progress.enqueue("working")
            await asyncio.sleep(0)
            assert await queue.get() == "body" and queue.empty() and not errors
        finally:
            loop.set_exception_handler(previous)

    asyncio.run(scenario())


def test_timeout_removes_getter_without_cancelling_producer():
    async def scenario():
        queue = asyncio.Queue(maxsize=1)
        producer = asyncio.get_running_loop().create_future()
        with pytest.raises(TimeoutError):
            await next_stream_item(queue, producer, timeout=0.01)
        assert not producer.done()
        await queue.put("next")
        assert await queue.get() == "next"
        producer.set_result(None)
        item = await next_stream_item(queue, producer)
        assert isinstance(item, RuntimeError), "missing terminal became success"

    asyncio.run(scenario())
