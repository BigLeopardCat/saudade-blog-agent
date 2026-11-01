"""Optional history must leave room for the current request's complete input."""

import pytest
from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage, SystemMessage

from agent.principal import SOURCE_ASSERTION, Principal
from agent.runtime.budget import BudgetExceeded, BudgetLimits, TaskBudget, budget_scope
from agent.runtime.message_content import estimate_tokens
from agent.runtime.prompt_budget import fit_history
from agent.runtime.session import ToolSession

ACTOR = Principal(7, "user", SOURCE_ASSERTION)


def consume(budget, tokens):
    ticket = budget.reserve([HumanMessage(content="Earlier work.")], output_limit=8)
    budget.settle(ticket, AIMessage(content="Done.", usage_metadata={
        "input_tokens": tokens - 1, "output_tokens": 1, "total_tokens": tokens,
    }))


def prompt(current=None):
    return [SystemMessage(content="System policy."),
            HumanMessage(content="旧问题" * 300,
                         additional_kwargs={"memory_actor": {"uid": 7, "role": "user"}}),
            AIMessage(content="旧答复" * 300),
            current or HumanMessage(content="现在的问题")]


def test_spending_remainder_does_not_silently_remove_authorized_history():
    budget = TaskBudget(BudgetLimits(context_tokens=5000, task_tokens=500))
    consume(budget, 300)
    original = prompt()
    fitted = fit_history(original, ACTOR, [], budget.limits, 64, budget=budget)
    assert fitted == original
    assert budget.tokens == 300 and budget.model_calls == 1
    with pytest.raises(BudgetExceeded, match="task_tokens"):
        budget.reserve(fitted, output_limit=64)


def test_input_view_does_not_change_when_only_task_spending_limit_changes():
    limits = BudgetLimits(context_tokens=5000, task_tokens=300)
    fitted = fit_history(prompt(), ACTOR, [], limits, 64)
    assert fitted == prompt()
    with pytest.raises(BudgetExceeded, match="task_tokens"):
        TaskBudget(limits).reserve(fitted, output_limit=64)


def test_per_call_input_budget_limits_history_even_with_a_large_model_window():
    original = [SystemMessage(content="System policy."),
                HumanMessage(content="旧问题" * 2000,
                             additional_kwargs={"memory_actor": {"uid": 7, "role": "user"}}),
                AIMessage(content="旧答复" * 2000),
                HumanMessage(content="现在的问题")]
    limits = BudgetLimits(context_tokens=1000000, input_tokens=2000,
                          task_tokens=100000)
    fitted = fit_history(original, ACTOR, [], limits, 64)
    assert fitted[0] is original[0] and fitted[-1] is original[-1]
    assert estimate_tokens(fitted) < estimate_tokens(original)
    assert estimate_tokens(fitted) + 64 <= limits.input_tokens
    TaskBudget(limits).reserve(fitted, output_limit=64)


def test_current_image_system_context_and_tool_schema_take_priority_over_history():
    current = HumanMessage(content=[{"type": "text", "text": "看这张图"}, {
        "type": "image_url", "image_url": {"url": "data:image/png;base64,AA"}}])
    attachment = HumanMessage(content="页面数据。", additional_kwargs={"memory_context": True})
    original = [*prompt(current)[:-1], attachment, current]
    menu = [{"type": "function", "function": {
        "name": "fixture_read", "description": "Read fixture data. " * 30,
        "parameters": {"type": "object", "properties": {}},
    }}]
    budget = TaskBudget(BudgetLimits(context_tokens=10000, input_tokens=3000,
                                        task_tokens=5000))
    consume(budget, 400)
    fitted = fit_history(original, ACTOR, menu, budget.limits, 64, budget=budget)
    assert fitted[0] is original[0]
    assert fitted[-1].content[:-1] == current.content
    assert fitted[-1].content[-1] == {"type": "text", "text": attachment.content}
    assert current.content == original[-1].content
    assert estimate_tokens(fitted) < estimate_tokens(original)
    budget.reserve(fitted, menu, output_limit=64)
    assert budget.tokens <= budget.limits.task_tokens


def test_required_input_that_exceeds_the_remainder_is_preserved_and_rejected():
    budget = TaskBudget(BudgetLimits(context_tokens=5000, task_tokens=500))
    consume(budget, 400)
    original = prompt(HumanMessage(content="当前输入" * 300))
    fitted = fit_history(original, ACTOR, [], budget.limits, 64, budget=budget)
    assert fitted == original
    with pytest.raises(BudgetExceeded, match="task_tokens"):
        budget.reserve(fitted, output_limit=64)
    assert budget.tokens == 400 and budget.model_calls == 1


def test_tool_session_passes_the_shared_budget_into_history_selection():
    class Capture:
        max_tokens = 64

        def __init__(self):
            self.prompts = []

        def bind_tools(self, tools, **kwargs):
            return self

        def stream(self, messages, **kwargs):
            self.prompts.append(list(messages))
            yield AIMessageChunk(content="已收到当前问题。")

    model = Capture()
    budget = TaskBudget(BudgetLimits(context_tokens=5000, task_tokens=500))
    consume(budget, 300)
    with budget_scope(budget):
        result = ToolSession(model, {"configurable": {"principal": ACTOR}}, {}, {}).run(prompt())
    assert result.execution["blocked"] == [{"reason": "task_tokens"}]
    assert not model.prompts and budget.model_calls == 1 and budget.tokens == 300


def test_provider_usage_calibration_is_used_by_the_model_view_selector():
    original, identity = prompt(), object()
    budget = TaskBudget(BudgetLimits(context_tokens=1000000, input_tokens=10000))
    ticket = budget.reserve(original, model_key=identity, output_limit=8)
    budget.settle(ticket, AIMessage(content="ok", usage_metadata={
        "input_tokens": 500, "output_tokens": 1, "total_tokens": 501}))
    budget.limits = BudgetLimits(context_tokens=1000000, input_tokens=1000)
    fitted = fit_history(original, ACTOR, [], budget.limits, 64,
                         budget=budget, model_key=identity)
    assert fitted == original and budget.input_tokens(fitted, model_key=identity) == 500
