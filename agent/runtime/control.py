"""Request cancellation and a monotonic deadline shared by worker jobs."""

import time
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from threading import Event

from agent.runtime.budget import TaskBudget, budget_scope
from models.session import session_scope


class AgentCancelled(Exception):
    """The caller stopped this request before the next operation."""


@dataclass(frozen=True)
class RequestControl:
    stop_event: Event
    deadline: float
    budget: TaskBudget = field(default_factory=TaskBudget)
    session_id: str | None = None

    def check(self, *, check_budget=True):
        if self.stop_event.is_set():
            raise AgentCancelled()
        if check_budget:
            self.budget.check_time()
        if time.monotonic() >= self.deadline:
            raise TimeoutError("Agent request deadline exceeded")


_current: ContextVar[RequestControl | None] = ContextVar("agent_request_control", default=None)


@contextmanager
def request_scope(control):
    token = _current.set(control)
    try:
        control.budget.enter_request(control.deadline)
        control.check()
        with budget_scope(control.budget), session_scope(control.session_id):
            yield
    finally:
        try:
            control.budget.leave_request()
        finally:
            _current.reset(token)


def check_active(*, check_budget=True):
    control = _current.get()
    if control is not None:
        control.check(check_budget=check_budget)


def model_options(timeout):
    """Return a transport timeout bounded by the remaining request time."""
    control = _current.get()
    if control is None:
        return {}
    control.check()
    deadline = min(control.deadline, control.budget.clock.deadline or control.deadline)
    return {"timeout": min(timeout, max(0.001, deadline - time.monotonic()))}
