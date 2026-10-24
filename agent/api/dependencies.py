"""Resolve application-owned resources and bounded chat admission from a request."""

import asyncio
import logging

from fastapi import HTTPException, Request

from config.settings import settings

logger = logging.getLogger(__name__)
STREAM_QUEUE_WAIT = 3.0


def require_runtime(request: Request):
    runtime = request.app.state.runtime
    if runtime is None:
        raise HTTPException(503, "Agent not initialised")
    return runtime


def require_workers(request: Request):
    workers = request.app.state.workers
    if workers is None or workers.closing:
        raise HTTPException(503, "Agent not initialised")
    return workers


async def try_acquire_slot(request: Request) -> bool:
    workers = request.app.state.workers
    if workers is None or workers.closing:
        return False
    slots = request.app.state.chat_slots
    try:
        await asyncio.wait_for(slots.acquire(), timeout=STREAM_QUEUE_WAIT)
        if workers.closing:
            slots.release()
            return False
        return True
    except TimeoutError:
        logger.warning("Chat admission full (%d permits)", settings.max_concurrent_streams)
        return False


def release_slot(request: Request) -> None:
    # A duplicate release is a programming error, surfaced by BoundedSemaphore.
    request.app.state.chat_slots.release()
