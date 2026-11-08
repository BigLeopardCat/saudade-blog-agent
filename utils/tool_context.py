"""Share the active tool call identity with infrastructure observations."""

from contextlib import contextmanager
from contextvars import ContextVar

_call: ContextVar[tuple[str | None, int | None]] = ContextVar(
    "tool_call_context", default=(None, None))


@contextmanager
def scope(call_id, round_index=None):
    token = _call.set((call_id, round_index))
    try:
        yield
    finally:
        _call.reset(token)


def current():
    return _call.get()
