"""Own request jobs and drain their real threads before closing service dependencies."""

import asyncio
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from agent.runtime.control import RequestControl
from agent.runtime.jobs import RequestJobs

logger = logging.getLogger(__name__)


class RequestWorkers:
    def __init__(self, *, max_workers=16, request_timeout=120):
        self.executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="agent")
        self.closing = False
        self._requests = set()
        self._shutdown = None
        self.request_timeout = request_timeout

    def warm_up(self, function):
        """Optional startup work shares executor shutdown and reports failures."""
        if self.closing:
            raise RuntimeError("Service workers are shutting down")
        future = self.executor.submit(function)
        future.add_done_callback(self._warmup_done)

    @staticmethod
    def _warmup_done(future):
        if not future.cancelled() and (error := future.exception()) is not None:
            logger.warning("Optional service warmup failed", exc_info=(
                type(error), error, error.__traceback__))

    def request(self, loop, release, *, control, on_complete=None):
        if self.closing:
            raise RuntimeError("Service workers are shutting down")

        def released():
            try:
                release()
            finally:
                self._requests.discard(jobs)

        jobs = RequestJobs(loop, released, control=control, on_complete=on_complete)
        self._requests.add(jobs)
        return jobs

    def stop(self):
        """Reject new groups, signal running jobs and cancel jobs still queued."""
        self.closing = True
        for jobs in list(self._requests):
            jobs.close()

    async def run(self, function, *args):
        """Track non-chat persistence operations even when their HTTP waiter disconnects."""
        jobs = self.request(asyncio.get_running_loop(), lambda: None, control=RequestControl(
            threading.Event(), time.monotonic() + self.request_timeout))
        try:
            return await jobs.submit(self.executor, function, *args)
        finally:
            jobs.close()

    async def _drain(self):
        requests = list(self._requests)
        self.stop()
        # Joining the executor must not block the event loop that workers use for delivery.
        await asyncio.to_thread(self.executor.shutdown, wait=True, cancel_futures=True)
        await asyncio.gather(*(jobs.wait_closed() for jobs in requests))

    async def aclose(self):
        """Finish resource cleanup before propagating cancellation of the shutdown caller."""
        if self._shutdown is None:
            self.stop()
            self._shutdown = asyncio.create_task(self._drain())
        cancelled = False
        while True:
            try:
                await asyncio.shield(self._shutdown)
                break
            except asyncio.CancelledError:
                if self._shutdown.cancelled():
                    raise
                cancelled = True
        if cancelled:
            raise asyncio.CancelledError()
