"""Nested model work shares complete-request capacity and leaves a usable closing call."""

from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from agent.runtime.budget import BudgetExceeded, BudgetLimits, TaskBudget
from agent.runtime.tool_context import plan_observation


def test_two_results_share_space_after_the_system_task_and_schema():
    budget = TaskBudget(BudgetLimits(context_tokens=6000, input_tokens=6000))
    model = object()
    menu = [{"type": "function", "function": {"name": "read", "description": "schema" * 160,
             "parameters": {"type": "object", "properties": {}}}}]
    calls = [{"name": "read", "args": {}, "id": f"read-{i}"} for i in range(2)]
    prompt = [HumanMessage(content="任务材料" * 200, id="task"),
              AIMessage(content="", tool_calls=calls)]
    session = SimpleNamespace(system_message=SystemMessage(content="系统约束" * 200),
        principal=None, transient={}, menu=menu, budget=budget, output_limit=256,
        model=model, history_access=None)
    first = plan_observation(session, prompt, calls[0], "task")
    assert not first.fits("正文" * 3000)
    text = "字" * max(0, first.max_tokens - 20)
    assert first.fits(text)
    messages = [session.system_message, *prompt, first.message(text)]
    second = plan_observation(session, [*prompt, first.message(text)], calls[1], "task")
    messages.append(second.message("字" * max(0, second.max_tokens - 20)))
    ticket = budget.reserve(messages, menu, output_limit=256, model_key=model)
    assert ticket.estimated_input + ticket.output_limit <= 6000
    assert first.completion_calls == second.completion_calls == 1


def test_rejected_subworkflow_does_not_poison_the_held_final_response():
    budget = TaskBudget(BudgetLimits(model_calls=2, task_tokens=500))
    prompt = [HumanMessage(content="request")]
    with budget.completion_headroom(tokens=100, model_calls=1):
        with pytest.raises(BudgetExceeded, match="model_calls"):
            budget.admit_plan([(prompt, 16), (prompt, 16)])
        with pytest.raises(BudgetExceeded, match="task_tokens"):
            budget.admit_plan([([HumanMessage(content="x" * 600)], 20)])
        assert budget.reason is None and budget.model_calls == budget.tokens == 0
    assert budget.reserve(prompt, output_limit=16)


def test_completion_headroom_is_released_on_cancellation():
    budget = TaskBudget(BudgetLimits(model_calls=2, task_tokens=500))
    with pytest.raises(RuntimeError, match="cancelled"):
        with budget.completion_headroom(tokens=450, model_calls=2):
            assert budget.output_capacity([HumanMessage(content="reply")]) == 0
            raise RuntimeError("cancelled")
    budget.admit_plan([([HumanMessage(content="reply")], 16)] * 2)
    assert budget.reason is None and budget.model_calls == budget.tokens == 0
