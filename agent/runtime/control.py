"""Request cancellation and a monotonic deadline shared by worker jobs."""

import time
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from threading import Event


class AgentCancelled(Exception):
    """The caller stopped this request before the next operation."""


@dataclass(frozen=True)
class RequestControl:
    stop_event: Event
    deadline: float

    def check(self):
        if self.stop_event.is_set():
            raise AgentCancelled()
        if time.monotonic() >= self.deadline:
            raise TimeoutError("Agent request deadline exceeded")


_current: ContextVar[RequestControl | None] = ContextVar("agent_request_control", default=None)


@contextmanager
def request_scope(control):
    token = _current.set(control)
    try:
        control.check()
        yield
    finally:
        _current.reset(token)


def check_active():
    control = _current.get()
    if control is not None:
        control.check()


def model_options(timeout):
    """Return a transport timeout bounded by the remaining request time."""
    control = _current.get()
    if control is None:
        return {}
    control.check()
    return {"timeout": min(timeout, max(0.001, control.deadline - time.monotonic()))}
