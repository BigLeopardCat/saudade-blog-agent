"""A lifespan-owned async HTTP pool for synchronous framework model calls."""

import logging
from contextlib import ExitStack, contextmanager
from contextvars import ContextVar
from queue import Empty, Full, Queue
from threading import Event, Lock
from time import monotonic

import anyio
import httpx
from anyio.from_thread import start_blocking_portal

from utils.trace import record

logger = logging.getLogger(__name__)
_current = ContextVar("model_io", default=None)
_responses = ContextVar("model_http_responses", default=None)
_cancel_check = ContextVar("model_cancel_check", default=None)


class ModelCleanupError(RuntimeError):
    """A model transport could not safely finish resource cleanup."""


class _ResponseStream(httpx.AsyncByteStream):
    def __init__(self, upstream):
        self.upstream = upstream
        self.closed = False

    def __aiter__(self):
        return self.upstream.__aiter__()

    async def aclose(self):
        if not self.closed:
            await self.upstream.aclose()
            self.closed = True


async def _track_response(response):
    responses = _responses.get()
    if responses is not None:
        response.stream = _ResponseStream(response.stream)
        responses.append(response)


class ModelIO:
    """Keep model I/O owned until actual exit; failed cleanup prevents pool reuse."""

    def __init__(self, *, transport=None, cleanup_timeout=2.0):
        if cleanup_timeout <= 0:
            raise ValueError("cleanup_timeout must be positive")
        self._transport = transport
        self._cleanup_timeout = cleanup_timeout
        self._resources = ExitStack()
        self._lock = Lock()
        self._closed = self._unhealthy = False
        self._portal = self._client = None
        self._streams = set()
        self._shutdown = None
        self._shutdown_complete = Event()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    @property
    def ready(self):
        return not (self._closed or self._unhealthy)

    @property
    def pending_cleanup(self):
        with self._lock:
            return sum(not event.is_set() for event in self._streams) + int(
                self._shutdown is not None and not self._shutdown_complete.is_set())

    def _quarantine(self, reason):
        with self._lock:
            self._unhealthy = True
        logger.error("Model transport unavailable: %s; pending_cleanup=%d",
                     reason, self.pending_cleanup)
        record("model", "transport_unavailable", reason=reason,
               pending_cleanup=self.pending_cleanup)

    @property
    def client(self):
        with self._lock:
            if self._closed:
                raise RuntimeError("Model transport is closed")
            if self._unhealthy:
                raise ModelCleanupError("Model transport cleanup failed; restart required")
            if self._portal is None:
                self._portal = self._resources.enter_context(start_blocking_portal())
                self._client = self._portal.call(lambda: httpx.AsyncClient(
                    transport=self._transport, event_hooks={"response": [_track_response]}))
            return self._client

    async def _close_pool(self):
        try:
            with anyio.move_on_after(self._cleanup_timeout, shield=True) as scope:
                await self._client.aclose()
            if scope.cancel_called:
                raise ModelCleanupError("Model HTTP pool cleanup deadline exceeded")
        finally:
            self._shutdown_complete.set()

    def close(self):
        with self._lock:
            self._closed = True
            streams = list(self._streams)
        deadline = monotonic() + self._cleanup_timeout
        if any(not event.wait(max(0, deadline - monotonic())) for event in streams):
            self._quarantine("stream_cleanup_pending")
            raise ModelCleanupError("Model streams still own resources during shutdown")
        with self._lock:
            if self._client is not None and self._shutdown is None:
                self._shutdown = self._portal.start_task_soon(self._close_pool)
        if self._shutdown is not None:
            if not self._shutdown_complete.wait(self._cleanup_timeout + 0.25):
                self._quarantine("pool_cleanup_pending")
                raise ModelCleanupError("Model HTTP pool still owns resources during shutdown")
            try:
                self._shutdown.result()
            finally:
                with self._lock:
                    resources = self._resources.pop_all()
                resources.close()
        else:
            self._resources.close()

    async def _finish_stream(self, stream, responses):
        complete = False
        try:
            with anyio.move_on_after(self._cleanup_timeout, shield=True) as scope:
                if stream is not None:
                    await stream.aclose()
                for response in responses:
                    await response.aclose()
                    # httpx marks is_closed before the underlying stream has closed.
                    await response.stream.aclose()
            complete = not scope.cancel_called
        finally:
            if not complete:
                self._quarantine("stream_cleanup_failed")

    def iterate(self, source):
        from agent.runtime.control import check_active

        check = _cancel_check.get() or check_active
        check()
        self.client
        queue, closed = Queue(maxsize=32), Event()
        responses = []

        async def produce(*, task_status=anyio.TASK_STATUS_IGNORED):
            token = _responses.set(responses)
            task_status.started()
            stream = None
            try:
                stream = source()
                async for chunk in stream:
                    while True:
                        try:
                            queue.put_nowait(chunk)
                            break
                        except Full:
                            await anyio.sleep(0.005)
            finally:
                try:
                    await self._finish_stream(stream, responses)
                finally:
                    _responses.reset(token)
                    closed.set()

        with self._lock:
            if not self.ready:
                raise ModelCleanupError("Model transport is unavailable")
            self._streams = {event for event in self._streams if not event.is_set()}
            self._streams.add(closed)
            future, _ = self._portal.start_task(produce)
        try:
            while True:
                check()
                try:
                    yield queue.get(timeout=0.05)
                except Empty:
                    if future.done():
                        future.result()
                        if self._unhealthy:
                            raise ModelCleanupError("Model stream cleanup failed")
                        break
        finally:
            started = monotonic()
            cancelled = not closed.is_set()
            if cancelled:
                future.cancel()
            finished = closed.wait(self._cleanup_timeout + 0.25)
            if not finished:
                self._quarantine("stream_cleanup_pending")
            record("model", "transport_closed", cancelled=cancelled,
                   cleanup_ms=round((monotonic() - started) * 1000, 2),
                   producer_exited=finished, cleanup_complete=finished and not self._unhealthy,
                   pending_cleanup=self.pending_cleanup,
                   responses=len(responses), responses_closed=all(
                       response.stream.closed for response in responses))


def current_io():
    return _current.get()


@contextmanager
def io_scope(model_io, *, cancel_check=None):
    token = _current.set(model_io) if model_io is not None else None
    check_token = _cancel_check.set(cancel_check) if cancel_check is not None else None
    try:
        yield
    finally:
        if check_token is not None:
            _cancel_check.reset(check_token)
        if token is not None:
            _current.reset(token)
