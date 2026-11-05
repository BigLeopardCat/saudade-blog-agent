"""Adapt native runtime events to HTTP turn results and thread-safe queue delivery."""

import asyncio
import logging
import threading
import time

from langchain_core.messages import AIMessageChunk

from agent.contracts.events import StreamEvent
from agent.principal import Principal
from agent.runtime.budget import current_budget
from agent.runtime.control import AgentCancelled
from agent.runtime.progress import ProgressEmitter
from agent.transport import collect_turn, emit_to_queue
from utils.trace import record

logger = logging.getLogger(__name__)


class NativeCancelled:
    """Worker cancellation result; it must not become a successful END frame."""


async def next_stream_item(queue, producer, *, timeout=2.0):
    """Drain ordered data before observing the producer's separate terminal result."""
    waiting = asyncio.create_task(queue.get())
    try:
        done, _ = await asyncio.wait((waiting, producer), timeout=timeout,
                                     return_when=asyncio.FIRST_COMPLETED)
        if waiting.done():
            return waiting.result()
        if not done:
            raise TimeoutError()
        if not queue.empty():
            return await waiting
        if producer.cancelled():
            return NativeCancelled()
        error = producer.exception()
        if error is not None:
            return NativeCancelled() if isinstance(error, AgentCancelled) else error
        result = producer.result()
        return (result if isinstance(result, (NativeCancelled, Exception)) else
                RuntimeError("Stream producer ended without a terminal frame"))
    finally:
        if not waiting.done():
            waiting.cancel()
        await asyncio.gather(waiting, return_exceptions=True)


def runtime_events(
    runtime,
    messages: list,
    *,
    thread_id: str,
    user_id: int,
    principal: Principal | None,
    conversation_id: int | None,
    confirm_grant: dict | None = None,
    stop_event=None,
    progress_emitter=None,
    task_id=None,
    resume_task=None,
    browser_feedback=False,
    browser_result=None,
    task_inputs=False,
):
    """Bind verified request coordinates to the native Runtime event contract."""
    stream_events = getattr(runtime, "stream_events", None)
    if not callable(stream_events):
        raise RuntimeError("native Runtime stream_events is not available")
    config = {
        "configurable": {
            "thread_id": thread_id,
            "user_id": user_id,
            "conversation_id": conversation_id,
            "principal": principal or Principal(uid=user_id),
            "stop_event": stop_event,
            "progress_emitter": progress_emitter,
            "budget": current_budget(),
        }
    }
    if browser_feedback:
        config["configurable"]["browser_feedback"] = True
    if task_inputs:
        config["configurable"]["task_inputs"] = True
    if browser_result is not None:
        config["configurable"]["browser_result"] = browser_result
    state: dict[str, object] = {"messages": messages}
    if confirm_grant is not None:
        state["confirm_grant"] = confirm_grant
    if task_id:
        state["task_id"] = task_id
    if resume_task:
        state["resume_task"] = resume_task
    return stream_events(state, config)


def run_turn(
    runtime,
    messages: list,
    thread_id: str,
    user_id: int = 0,
    principal: Principal | None = None,
    conversation_id: int | None = None,
    task_id=None,
    resume_task=None,
    browser_feedback=False,
    browser_result=None,
) -> tuple[str, str, list]:
    """Run one native turn and return the legacy non-streaming response shape."""
    turn = collect_turn(
        runtime_events(
            runtime,
            messages,
            thread_id=thread_id,
            user_id=user_id,
            principal=principal,
            conversation_id=conversation_id,
            task_id=task_id,
            resume_task=resume_task,
            browser_feedback=browser_feedback,
            browser_result=browser_result,
        )
    )
    return turn.reply, turn.navigation, turn.execution


def stream_to_queue(
    runtime,
    messages: list,
    thread_id: str,
    queue: asyncio.Queue,
    loop,
    user_id: int = 0,
    stop_event: threading.Event | None = None,
    principal: Principal | None = None,
    confirm_grant: dict | None = None,
    conversation_id: int | None = None,
    progress_emitter: ProgressEmitter | None = None,
    *,
    task_id=None,
    resume_task=None,
    browser_feedback=False,
    browser_result=None,
    task_inputs=False,
):
    """Run native events in the worker and enqueue protocol-safe values.

    Queue insertion is the only async boundary here. Runtime exceptions are
    delivered via the worker Future, so cancellation cannot be blocked behind
    buffered output. Only a normal stream puts the terminal sentinel.
    """
    visible_parts = []

    def put(value):
        if loop.is_closed():
            raise AgentCancelled()
        if stop_event is not None and stop_event.is_set():
            raise AgentCancelled()
        pending = queue.put(value)
        try:
            future = asyncio.run_coroutine_threadsafe(pending, loop)
        except RuntimeError:
            pending.close()
            raise AgentCancelled() from None
        try:
            deadline = time.monotonic() + 5
            while True:
                if stop_event is not None and stop_event.is_set():
                    raise AgentCancelled()
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    record("producer", "backpressure_timeout", buffer_limit=queue.maxsize)
                    raise AgentCancelled()
                try:
                    future.result(timeout=min(0.05, remaining))
                    break
                except TimeoutError:
                    continue
            if isinstance(value, AIMessageChunk):
                visible_parts.append(str(value.content or ""))
            elif isinstance(value, StreamEvent) and value.name == "text_reset":
                visible_parts.clear()
        finally:
            if not future.done():
                future.cancel()

    try:
        events = runtime_events(
            runtime,
            messages,
            thread_id=thread_id,
            user_id=user_id,
            principal=principal,
            conversation_id=conversation_id,
            confirm_grant=confirm_grant,
            stop_event=stop_event,
            progress_emitter=progress_emitter,
            task_id=task_id,
            resume_task=resume_task,
            browser_feedback=browser_feedback,
            browser_result=browser_result,
            task_inputs=task_inputs,
        )
        emit_to_queue(events, put)
        put(None)
    except AgentCancelled:
        logger.info("[stream] native runtime cancelled")
        return NativeCancelled()
    except Exception as exc:
        logger.exception("[stream] native runtime producer failed: %s", exc)
        return exc
    finally:
        record("producer", "stream_end", reply="".join(visible_parts))
