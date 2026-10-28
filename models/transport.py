"""A lifespan-owned async HTTP pool for synchronous framework model calls."""

from contextlib import ExitStack, contextmanager
from contextvars import ContextVar
from queue import Empty, Full, Queue
from threading import Event, Lock
from time import monotonic

import anyio
import httpx
from anyio.from_thread import start_blocking_portal

from utils.trace import record

_current = ContextVar("model_io", default=None)
_responses = ContextVar("model_http_responses", default=None)
_cancel_check = ContextVar("model_cancel_check", default=None)


class _ResponseStream(httpx.AsyncByteStream):
    def __init__(self, upstream):
        self.upstream = upstream

    def __aiter__(self):
        return self.upstream.__aiter__()

    async def aclose(self):
        with anyio.CancelScope(shield=True):
            await self.upstream.aclose()


async def _track_response(response):
    responses = _responses.get()
    if responses is not None:
        response.stream = _ResponseStream(response.stream)
        responses.append(response)


class ModelIO:
    """Use AnyIO's standard portal; the Agent and checkpoint stay on their owning worker."""

    def __init__(self, *, transport=None):
        self._transport = transport
        self._resources = ExitStack()
        self._lock = Lock()
        self._closed = False
        self._portal = None
        self._client = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    @property
    def client(self):
        with self._lock:
            if self._closed:
                raise RuntimeError("Model transport is closed")
            if self._portal is None:
                self._portal = self._resources.enter_context(start_blocking_portal())
                self._client = self._portal.call(lambda: httpx.AsyncClient(
                    transport=self._transport, event_hooks={"response": [_track_response]}))
                self._resources.callback(self._portal.call, self._client.aclose)
            return self._client

    def close(self):
        with self._lock:
            self._closed = True
            self._resources.close()

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
                    with anyio.CancelScope(shield=True):
                        if stream is not None:
                            await stream.aclose()
                        for response in responses:
                            if not response.is_closed:
                                await response.aclose()
                finally:
                    _responses.reset(token)
                    closed.set()

        future, _ = self._portal.start_task(produce)
        try:
            while True:
                check()
                try:
                    yield queue.get(timeout=0.05)
                except Empty:
                    if future.done():
                        future.result()
                        break
        finally:
            started = monotonic()
            cancelled = not future.done()
            if cancelled:
                future.cancel()
            closed.wait()
            record("model", "transport_closed", cancelled=cancelled,
                   cleanup_ms=round((monotonic() - started) * 1000, 2),
                   responses=len(responses), responses_closed=all(r.is_closed for r in responses))


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
