"""Resolve application-owned resources and bounded chat admission from a request."""

import logging

from fastapi import HTTPException, Request

from config.settings import settings

logger = logging.getLogger(__name__)


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


async def try_acquire_slot(request: Request, *, session_id: str | None = None) -> bool:
    workers = request.app.state.workers
    if workers is None or workers.closing:
        return False
    if session_id and not await workers.wait_for_session(session_id):
        logger.warning("Previous conversation request has not finished releasing resources")
        return False
    slots = request.app.state.chat_slots
    if slots.locked():
        logger.warning("Chat admission full (%d permits)", settings.max_concurrent_streams)
        return False
    # The uncontended Semaphore fast path does not suspend this event loop.
    await slots.acquire()
    if workers.closing:
        slots.release()
        return False
    return True


def release_slot(request: Request) -> None:
    # A duplicate release is a programming error, surfaced by BoundedSemaphore.
    request.app.state.chat_slots.release()
