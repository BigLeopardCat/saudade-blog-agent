"""Own the application runtime and checkpoint resources for one evaluation process."""

import asyncio
import json
import os
import time
from contextlib import ExitStack

from config.settings import check_retired_runtime

ENGINE = "langchain"
REPORTS_DIR = "eval/report/runs"

_runtime = None
_resources = ExitStack()


def assertion_frame(value):
    """Normalize typed controls to the stored golden report format, outside production."""
    from agent.contracts.events import StreamEvent

    if not isinstance(value, StreamEvent):
        return value
    prefixes = {"progress": "__PROCESS__:", "confirmation": "__CONFIRM__:",
                "pending": "__PENDING__:", "command": "__CMD__:", "receipts": "__EXEC__:",
                "task_input": "__INPUT__:", "text_reset": "__TEXT_RESET__:"}
    body = value.data if value.name == "progress" else json.dumps(value.data, ensure_ascii=False)
    return prefixes[value.name] + body


def close_agent():
    global _runtime
    _runtime = None
    _resources.close()


def check_environment():
    check_retired_runtime(os.environ.get("GOLDEN_ARM"), "GOLDEN_ARM")


def build_agent(*, resources):
    from agent.composition import build

    check_environment()
    return build(resources=resources)


def ensure_agent():
    global _runtime
    if _runtime is None:
        t0 = time.time()
        # Use the same application composition as the service.
        with ExitStack() as startup:
            _runtime = build_agent(resources=startup)
            _resources.enter_context(startup.pop_all())
        print(f"[init] Agent runtime ready: {time.time() - t0:.1f}s")
    return _runtime


async def collect_frames(queue, producer):
    """Drain ordered frames and observe producer failures exactly once.

    A failed request remains a result for evaluation; awaiting its task must not
    re-raise the same exception and discard the complete run report.
    """
    from agent.api.turns import ProducerCancelled, next_stream_item

    frames = []
    while True:
        item = await next_stream_item(queue, producer, timeout=None)
        if item is None:
            break
        if isinstance(item, ProducerCancelled):
            frames.append(RuntimeError("Evaluation producer cancelled"))
            break
        frames.append(item)
        if isinstance(item, BaseException):
            break
    completed, = await asyncio.gather(producer, return_exceptions=True)
    if isinstance(completed, BaseException) and not any(item is completed for item in frames):
        frames.append(completed)
    return frames


async def collect_request(runtime, req, principal, confirm_grant, thread):
    """Forward the same request controls as the production stream adapter."""
    from agent import runtime_probe
    from agent.api.context import build_messages
    from agent.api.turns import stream_to_queue

    queue = asyncio.Queue(maxsize=64)
    producer = asyncio.create_task(asyncio.to_thread(
        stream_to_queue, runtime,
        build_messages(req, holds_context=runtime_probe.holds_context(runtime)),
        thread, queue, asyncio.get_running_loop(), req.user_id, None, principal,
        confirm_grant, req.conversation_id,
        task_id=req.task_id, turn_id=req.turn_id, resume_task=req.resume_task,
        browser_feedback=req.browser_feedback,
        browser_result=req.browser_result.model_dump() if req.browser_result else None,
        task_inputs=req.task_inputs,
        image_event=req.image_event.model_dump() if req.image_event else None))
    return await collect_frames(queue, producer)
