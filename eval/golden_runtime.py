"""Own the native evaluation runtime and its checkpoint resources for one CLI process."""

import asyncio
import os
import time
from contextlib import ExitStack

from config.settings import check_retired_runtime

ENGINE = "native"
REPORTS_DIR = "eval/report/runs"

_runtime = None
_resources = ExitStack()


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
        # Evaluation uses the same native runtime factory as the service.
        with ExitStack() as startup:
            _runtime = build_agent(resources=startup)
            _resources.enter_context(startup.pop_all())
        print(f"[init] Native runtime ready: {time.time() - t0:.1f}s")
    return _runtime


async def collect_frames(queue, producer):
    """Drain ordered frames and observe producer failures exactly once.

    A failed request remains a result for evaluation; awaiting its task must not
    re-raise the same exception and discard the complete run report.
    """
    from agent.api.turns import NativeCancelled, next_stream_item

    frames = []
    while True:
        item = await next_stream_item(queue, producer, timeout=None)
        if item is None:
            break
        if isinstance(item, NativeCancelled):
            frames.append(RuntimeError("Evaluation producer cancelled"))
            break
        frames.append(item)
        if isinstance(item, BaseException):
            break
    completed, = await asyncio.gather(producer, return_exceptions=True)
    if isinstance(completed, BaseException) and not any(item is completed for item in frames):
        frames.append(completed)
    return frames
