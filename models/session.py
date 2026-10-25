"""Provider routing affinity shared by model calls in one conversation."""

from contextlib import contextmanager
from contextvars import ContextVar

_current: ContextVar[str | None] = ContextVar("model_session", default=None)


def current_session():
    return _current.get()


@contextmanager
def session_scope(session_id):
    token = _current.set(session_id)
    try:
        yield
    finally:
        _current.reset(token)
