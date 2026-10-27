"""Optional history must leave room for the current request's complete input."""

import pytest
from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage, SystemMessage

from agent.principal import UNKNOWN
from agent.runtime.budget import BudgetExceeded, BudgetLimits, RequestBudget, budget_scope
from agent.runtime.message_content import estimate_tokens
from agent.runtime.prompt_budget import fit_history
from agent.runtime.session import ToolSession


def consume(budget, tokens):
    ticket = budget.reserve([HumanMessage(content="Earlier work.")], output_limit=8)
    budget.settle(ticket, AIMessage(content="Done.", usage_metadata={
        "input_tokens": tokens - 1, "output_tokens": 1, "total_tokens": tokens,
    }))


def prompt(current=None):
    return [SystemMessage(content="System policy."),
            HumanMessage(content="旧问题" * 300), AIMessage(content="旧答复" * 300),
            current or HumanMessage(content="现在的问题")]


def test_remaining_request_budget_limits_history_before_the_first_model_call():
    budget = RequestBudget(BudgetLimits(context_tokens=5000, request_tokens=500))
    consume(budget, 300)
    original = prompt()
    fitted = fit_history(original, UNKNOWN, [], budget.limits, 64, budget=budget)
    assert fitted[0] is original[0] and fitted[-1] is original[-1]
    assert 0 < estimate_tokens(fitted) < estimate_tokens(original)
    assert budget.tokens == 300 and budget.model_calls == 1
    budget.reserve(fitted, output_limit=64)
    assert budget.tokens <= budget.limits.request_tokens


def test_fresh_request_uses_the_smaller_request_limit_even_without_a_budget_argument():
    limits = BudgetLimits(context_tokens=5000, request_tokens=300)
    fitted = fit_history(prompt(), UNKNOWN, [], limits, 64)
    RequestBudget(limits).reserve(fitted, output_limit=64)
    assert estimate_tokens(fitted) + 64 <= limits.request_tokens


def test_current_image_system_context_and_tool_schema_take_priority_over_history():
    current = HumanMessage(content=[{"type": "text", "text": "看这张图"}, {
        "type": "image_url", "image_url": {"url": "data:image/png;base64,AA"}}])
    attachment = HumanMessage(content="页面数据。", additional_kwargs={"memory_context": True})
    original = [*prompt(current)[:-1], attachment, current]
    menu = [{"type": "function", "function": {
        "name": "fixture_read", "description": "Read fixture data. " * 30,
        "parameters": {"type": "object", "properties": {}},
    }}]
    budget = RequestBudget(BudgetLimits(context_tokens=10000, request_tokens=3500))
    consume(budget, 400)
    fitted = fit_history(original, UNKNOWN, menu, budget.limits, 64, budget=budget)
    assert fitted[0] is original[0] and fitted[-1] is current
    assert attachment in fitted and fitted.index(attachment) < len(fitted) - 1
    assert estimate_tokens(fitted) < estimate_tokens(original)
    budget.reserve(fitted, menu, output_limit=64)
    assert budget.tokens <= budget.limits.request_tokens


def test_required_input_that_exceeds_the_remainder_is_preserved_and_rejected():
    budget = RequestBudget(BudgetLimits(context_tokens=5000, request_tokens=500))
    consume(budget, 400)
    original = prompt(HumanMessage(content="当前输入" * 300))
    fitted = fit_history(original, UNKNOWN, [], budget.limits, 64, budget=budget)
    assert fitted == [original[0], original[-1]]
    with pytest.raises(BudgetExceeded, match="request_tokens"):
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
    budget = RequestBudget(BudgetLimits(context_tokens=5000, request_tokens=500))
    consume(budget, 300)
    with budget_scope(budget):
        result = ToolSession(model, {}, {}, {}).run(prompt())
    assert result.reply == "已收到当前问题。"
    assert len(model.prompts) == 1 and model.prompts[0][-1].content == "现在的问题"
    assert budget.model_calls == 2 and budget.tokens <= budget.limits.request_tokens
