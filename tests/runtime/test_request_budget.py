"""Complete request estimates and usage accounting cannot reset across engines."""

import json
import threading
import time

import httpx
import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.tools import tool

from agent.runtime.budget import (
    BudgetExceeded,
    BudgetLimits,
    RequestBudget,
    budget_scope,
    current_budget,
)
from agent.runtime.control import RequestControl, request_scope
from agent.runtime.model_budget import tool_callbacks
from agent.runtime.session import ToolSession
from models.llm import get_llm
from tools.result import ok


@tool
def fixture_read() -> str:
    """Read a small fixture resource."""
    return ok("source")


def schema(description):
    return {"type": "function", "function": {
        "name": "fixture", "description": description,
        "parameters": {"type": "object", "properties": {}},
    }}


def test_input_budget_is_separate_from_the_model_window():
    limits = BudgetLimits(context_tokens=1000000, input_tokens=1000)
    assert limits.input_tokens == 1000
    budget = RequestBudget(limits)
    with pytest.raises(BudgetExceeded, match="input_tokens"):
        budget.reserve([HumanMessage(content="x" * 3000)], output_limit=10)
    assert budget.model_calls == budget.tokens == 0

    budget = RequestBudget(BudgetLimits(context_tokens=1000000, input_tokens=100000))
    ticket = budget.reserve([HumanMessage(content="x" * 3000)], output_limit=10)
    assert ticket.estimated_input > 0


def test_input_budget_never_exceeds_the_model_window():
    assert BudgetLimits(context_tokens=500, input_tokens=64000).input_tokens == 500


def test_schema_and_tool_call_arguments_are_part_of_context_with_output_reserve():
    limits = BudgetLimits(context_tokens=1000)
    tiny = [HumanMessage(content="hello")]
    for messages, tools in ((tiny, [schema("x" * 8000)]), (
        [*tiny, AIMessage(content="", tool_calls=[
            {"name": "fixture", "args": {"body": "x" * 8000}, "id": "large"}])], [])):
        budget = RequestBudget(limits)
        with pytest.raises(BudgetExceeded, match="context_tokens"):
            budget.reserve(messages, tools, output_limit=10)
        assert budget.model_calls == budget.tokens == 0
    budget = RequestBudget(BudgetLimits(context_tokens=100))
    with pytest.raises(BudgetExceeded):
        budget.reserve(tiny, output_limit=100)


def test_provider_usage_settles_once_and_unknown_usage_keeps_the_reservation():
    budget = RequestBudget()
    ticket = budget.reserve([HumanMessage(content="hi")], output_limit=32)
    estimated = budget.tokens
    budget.settle(ticket, AIMessage(content="ok"))
    assert budget.tokens == estimated
    budget.settle(ticket, AIMessage(content="ok", usage_metadata={
        "input_tokens": 1, "output_tokens": 1, "total_tokens": 2}))
    assert budget.tokens == estimated
    ticket = budget.reserve([HumanMessage(content="hi")], output_limit=32)
    budget.settle(ticket, AIMessage(content="ok", usage_metadata={
        "input_tokens": 4, "output_tokens": 2, "total_tokens": 6,
        "input_token_details": {"cache_read": 4}}))
    assert budget.tokens == estimated + 6


def test_actual_usage_overrun_stops_future_calls_and_scope_does_not_reset_usage():
    budget = RequestBudget(BudgetLimits(request_tokens=100))
    with budget_scope(budget):
        ticket = budget.reserve([HumanMessage(content="hi")], output_limit=10)
    budget.settle(ticket, AIMessage(content="ok", usage_metadata={
        "input_tokens": 101, "output_tokens": 1, "total_tokens": 102}))
    with budget_scope(budget), pytest.raises(BudgetExceeded, match="request_tokens"):
        current_budget().reserve([HumanMessage(content="again")], output_limit=10)
    assert budget.tokens == 102 and current_budget() is None


def model(client):
    return get_llm(model="gpt-4o-mini", api_key="test-only", base_url="https://example.test/v1",
                   http_client=client, streaming=False, max_tokens=16, enable_thinking=False)


def response(request):
    return httpx.Response(200, json={
        "id": "fixture", "model": "gpt-4o-mini", "object": "chat.completion",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"},
                     "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 7, "completion_tokens": 1, "total_tokens": 8},
    })


def test_native_legacy_and_summary_share_model_budget_without_double_charging():
    requests = []
    budget = RequestBudget(BudgetLimits(model_calls=2))
    control = RequestControl(threading.Event(), time.monotonic() + 10, budget)

    def handle(request):
        requests.append(json.loads(request.content))
        return response(request)

    with httpx.Client(transport=httpx.MockTransport(handle)) as client, request_scope(control):
        native = ToolSession(model(client), {}, {}, {})
        assert native.run([HumanMessage(content="hello")]).reply == "ok"
        assert budget.model_calls == 1 and budget.tokens == 8
        assert model(client).invoke([HumanMessage(content="legacy")]).content == "ok"
        assert budget.model_calls == 2 and budget.tokens == 16
        with pytest.raises(BudgetExceeded, match="model_calls"):
            model(client).invoke([HumanMessage(content="summary")])
    assert len(requests) == 2


def test_callback_rejects_large_schema_before_transport_and_disables_retries():
    requests = []

    def handle(request):
        requests.append(request)
        return response(request)

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        with budget_scope(RequestBudget(BudgetLimits(context_tokens=1000))):
            llm = model(client)
            assert llm.max_retries == 0
            with pytest.raises(BudgetExceeded, match="context_tokens"):
                llm.bind_tools([schema("x" * 8000)]).invoke([HumanMessage(content="hello")])
    assert not requests


def test_legacy_tools_share_attempt_budget_and_native_tools_count_only_once():
    budget = RequestBudget(BudgetLimits(tool_calls=1))
    with budget_scope(budget):
        config = {"callbacks": tool_callbacks()}
        assert fixture_read.invoke({}, config=config) == "source"
        with pytest.raises(BudgetExceeded, match="tool_calls"):
            fixture_read.invoke({}, config=config)
    assert budget.tool_calls == 1

    class Model:
        replies = iter([AIMessage(content="", tool_calls=[
            {"name": "fixture_read", "args": {}, "id": "read"}]), AIMessage(content="done")])

        def bind_tools(self, tools, **kwargs):
            return self

        def invoke(self, prompt):
            return next(self.replies)

    budget = RequestBudget(BudgetLimits(tool_calls=1))
    with budget_scope(budget):
        result = ToolSession(Model(), {"callbacks": tool_callbacks()},
                             {"fixture_read": fixture_read}, {}).run([])
    assert result.reply == "done" and budget.tool_calls == 1


def test_read_progress_is_retained_in_native_result_instead_of_restarted_in_legacy():
    class Model:
        replies = iter([AIMessage(content="", tool_calls=[
            {"name": "fixture_read", "args": {}, "id": "read"}]),
            AIMessage(content="继续处理已读取的结果。")])

        def bind_tools(self, tools, **kwargs):
            return self

        def invoke(self, prompt):
            return next(self.replies)

    session = ToolSession(Model(), {}, {"fixture_read": fixture_read}, {})
    result = session.run([HumanMessage(content="read and continue")])
    assert result is not None and session.observations
    assert session.calls_used == 1 and session.budget.model_calls == 2


def test_provider_reasoning_replay_is_reserved_before_the_next_model_call():
    message = AIMessage(content="short", additional_kwargs={"reasoning_content": "x" * 6000})
    budget = RequestBudget(BudgetLimits(context_tokens=1000))
    with pytest.raises(BudgetExceeded, match="context_tokens"):
        budget.reserve([message], output_limit=32)
    assert budget.model_calls == budget.tokens == 0
