"""Active task time survives requests, waiting and process-loss reservations."""

from threading import Event
from types import SimpleNamespace

import pytest

from agent.runtime import control, task_time
from agent.runtime.budget import BudgetExceeded, BudgetLimits, TaskBudget
from agent.runtime.task_budget import MemoryUsage

TASK = {"id": "task", "scope": {"conversation_id": 42, "uid": 7, "role": "user"}}


@pytest.fixture
def clock(monkeypatch):
    now = [0.0]
    fake = SimpleNamespace(monotonic=lambda: now[0])
    monkeypatch.setattr(task_time, "time", fake)
    monkeypatch.setattr(control, "time", fake)
    return now


def selected(seconds=50):
    return TaskBudget(BudgetLimits(task_seconds=seconds))


def test_only_active_time_counts_and_remaining_time_bounds_transports(clock):
    storage, first = MemoryUsage(), selected()
    with control.request_scope(control.RequestControl(Event(), 100, first)):
        first.bind_task(storage, TASK)
        assert storage.load(TASK)["active_seconds"] == 50
        clock[0] = 5
    assert storage.load(TASK)["active_seconds"] == 5
    clock[0] = 1000
    second = selected()
    with control.request_scope(control.RequestControl(Event(), 1100, second)):
        second.bind_task(storage, TASK, create=False)
        assert control.model_options(90)["timeout"] == 45
        clock[0] = 1045
        with pytest.raises(BudgetExceeded, match="task_seconds"):
            control.check_active()
    assert storage.load(TASK)["active_seconds"] == 50


def test_process_loss_keeps_the_reserved_request_time(clock):
    storage, interrupted = MemoryUsage(), selected()
    interrupted.enter_request(20)
    interrupted.bind_task(storage, TASK)
    assert storage.load(TASK)["active_seconds"] == 20
    # A reconstructed worker never receives the old object's normal finalizer.
    clock[0] = 100
    recovered = selected()
    with control.request_scope(control.RequestControl(Event(), 200, recovered)):
        recovered.bind_task(storage, TASK, create=False)
        assert control.model_options(90)["timeout"] == 30


def test_nested_scope_settles_once_and_cancellation_keeps_elapsed_time(clock):
    storage, budget = MemoryUsage(), selected()
    stop = Event()
    request = control.RequestControl(stop, 100, budget)
    with pytest.raises(control.AgentCancelled):
        with control.request_scope(request):
            budget.bind_task(storage, TASK)
            with control.request_scope(request):
                clock[0] = 5
            assert storage.load(TASK)["active_seconds"] == 50
            clock[0] = 10
            stop.set()
            control.check_active()
    assert storage.load(TASK)["active_seconds"] == 10


def test_token_settlement_does_not_erase_the_time_reservation(clock):
    from langchain_core.messages import HumanMessage

    storage, budget = MemoryUsage(), selected()
    with control.request_scope(control.RequestControl(Event(), 100, budget)):
        budget.bind_task(storage, TASK)
        budget.reserve([HumanMessage(content="fixture")], output_limit=8)
        assert storage.load(TASK)["active_seconds"] == 50
        clock[0] = 3
    assert storage.load(TASK)["active_seconds"] == 3
    assert storage.load(TASK)["model_calls"] == 1


def test_unknown_historical_time_cannot_restart_a_task_from_zero(clock):
    storage = MemoryUsage()
    storage.load(TASK)
    budget = selected()
    with control.request_scope(control.RequestControl(Event(), 100, budget)):
        budget.bind_task(storage, TASK, create=False)
        with pytest.raises(BudgetExceeded, match="task_time_unknown"):
            control.check_active()
        assert storage.load(TASK)["active_seconds"] == budget.limits.task_seconds
