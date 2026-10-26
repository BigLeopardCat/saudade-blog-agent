"""Adapt native runtime events to HTTP turn results and thread-safe queue delivery."""

import asyncio
import logging
import threading

from langchain_core.messages import AIMessageChunk

from agent.principal import Principal
from agent.runtime.budget import current_budget
from agent.runtime.control import AgentCancelled
from agent.runtime.progress import ProgressEmitter
from agent.transport import collect_turn, emit_to_queue
from utils.trace import record

logger = logging.getLogger(__name__)


class NativeCancelled:
    """Worker cancellation sentinel; it must not become a successful END frame."""


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
):
    """Run native events in the worker and enqueue protocol-safe values.

    Queue insertion is the only async boundary here. Runtime exceptions are
    delivered before the terminal sentinel, so the HTTP adapter cannot emit a
    successful end frame after an error or cancellation.
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
            future.result(timeout=5)
            if isinstance(value, AIMessageChunk):
                visible_parts.append(str(value.content or ""))
        except TimeoutError:
            future.cancel()
            raise AgentCancelled() from None

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
        )
        emit_to_queue(events, put)
        put(None)
    except AgentCancelled:
        logger.info("[stream] native runtime cancelled")
        if not loop.is_closed():
            loop.call_soon_threadsafe(queue.put_nowait, NativeCancelled())
    except Exception as exc:
        logger.exception("[stream] native runtime producer failed: %s", exc)
        put(exc)
        try:
            put(None)
        except (AgentCancelled, RuntimeError):
            logger.info("[stream] producer sentinel consumer already closed")
    finally:
        record("producer", "stream_end", reply="".join(visible_parts))
