"""Keep a request's admission permit until its actual executor jobs exit."""

import asyncio
import contextvars
import logging
import threading
import time
from contextlib import ExitStack

from agent.runtime.control import request_scope
from utils.trace import record

logger = logging.getLogger(__name__)


class RequestJobs:
    def __init__(self, loop, release, *, control, on_complete=None):
        self.loop = loop
        self.release = release
        self.control = control
        self.on_complete = on_complete
        self._context = contextvars.copy_context()
        self._lock = threading.Lock()
        self._futures = set()
        self._closed = False
        self._finished = False
        self._started = time.monotonic()
        self._closed_at = None
        self._drained = asyncio.Event()

    def submit(self, executor, function, *args):
        context = contextvars.copy_context()

        def run():
            with request_scope(self.control):
                return function(*args)

        with self._lock:
            if self._closed:
                raise RuntimeError("Cannot submit work after the request closed")
            future = executor.submit(context.run, run)
            self._futures.add(future)
        future.add_done_callback(self._done)
        result = asyncio.wrap_future(future, loop=self.loop)
        # Detached consumers must still retrieve failures; awaiting keeps its normal semantics.
        result.add_done_callback(lambda task: None if task.cancelled() else task.exception())
        return result

    def _done(self, future):
        with self._lock:
            self._futures.discard(future)
        self._finish_if_drained()

    def close(self):
        self.control.stop_event.set()
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._closed_at = time.monotonic()
            futures = list(self._futures)
        record("runtime", "consumer_closed", running_jobs=sum(f.running() for f in futures),
               pending_jobs=sum(not f.running() and not f.done() for f in futures))
        for future in futures:
            future.cancel()
        self._finish_if_drained()

    def _finish_if_drained(self):
        with self._lock:
            if not self._closed or self._futures or self._finished:
                return
            self._finished = True
        try:
            self.loop.call_soon_threadsafe(self._finish, context=self._context)
        except RuntimeError:
            logger.exception("Request jobs drained after the event loop closed")

    def _finish(self):
        with ExitStack() as cleanup:
            cleanup.callback(self._drained.set)
            cleanup.callback(self.release)
            if self.on_complete is not None:
                cleanup.callback(self.on_complete)
            self.control.budget.trace_usage()
            now = time.monotonic()
            record("runtime", "jobs_drained", duration_s=round(now - self._started, 3),
                   detached_s=round(now - self._closed_at, 3))

    async def wait_closed(self):
        """Wait until worker exit, trace finalization and permit release have completed."""
        await self._drained.wait()
