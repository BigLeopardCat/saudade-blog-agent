"""Resource limits stop the framework loop without a hidden narration request."""

import json

import httpx
import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.tools import StructuredTool

from agent.runtime.budget import BudgetLimits, RequestBudget, budget_scope
from agent.runtime.progress import ProgressEmitter
from agent.runtime.session import ToolSession
from models.llm import get_llm
from tools.result import ok


def completion(content="", calls=None, input_tokens=20):
    return {"id": "fixture", "model": "fixture-model", "object": "chat.completion",
            "choices": [{"index": 0, "finish_reason": "tool_calls" if calls else "stop",
                         "message": {"role": "assistant", "content": content,
                                     **({"tool_calls": calls} if calls else {})}}],
            "usage": {"prompt_tokens": input_tokens, "completion_tokens": 10,
                      "total_tokens": input_tokens + 10}}


def tool_call(identifier="read"):
    return {"id": identifier, "type": "function", "function": {
        "name": "fixture_read", "arguments": "{}"}}


@pytest.mark.parametrize("limit", ["request_tokens", "context_tokens", "model_calls",
                                   "tool_calls", "none"])
def test_real_sdk_stops_at_resource_boundary_without_an_extra_narrator(limit, monkeypatch):
    requests, reads, progress, phases = [], [], [], []
    from agent.runtime import trace_context
    monkeypatch.setattr(trace_context, "record", lambda node, event, **data: phases.append(data))

    def read_fixture() -> str:
        reads.append("read")
        return ok("VERIFIED_FACT: 42" + ("x" * 3000 if limit == "context_tokens" else ""))

    reader = StructuredTool.from_function(
        read_fixture, name="fixture_read", description="Fixture read. " + "x" * 16000)

    def handle(request):
        body = json.loads(request.content)
        requests.append(body)
        assert body["tools"] and body["tool_choice"] == "auto"
        if len(requests) == 1:
            return httpx.Response(200, json=completion(
                calls=[tool_call()], input_tokens=4000 if limit == "context_tokens" else 7000))
        assert len(requests) == 2 and limit in {"none", "tool_calls"}
        assert any(message["role"] == "tool" and "VERIFIED_FACT: 42" in message["content"]
                   for message in body["messages"])
        return httpx.Response(200, json=completion(
            "查到的结果是 42。" if limit == "none" else "",
            [tool_call("over-budget")] if limit == "tool_calls" else None))

    limits = BudgetLimits(
        context_tokens=5000 if limit == "context_tokens" else 16384,
        request_tokens=9000 if limit == "request_tokens" else 32768,
        model_calls=1 if limit == "model_calls" else 8,
        tool_calls=1 if limit == "tool_calls" else 8)
    budget = RequestBudget(limits)
    with httpx.Client(transport=httpx.MockTransport(handle)) as client, budget_scope(budget):
        model = get_llm(api_key="test-only", base_url="https://example.test/v1",
                        model="fixture-model", http_client=client, streaming=False,
                        max_tokens=64, enable_thinking=False)
        config = {"configurable": {"progress_emitter": ProgressEmitter(progress.append)}}
        result = ToolSession(model, config, {reader.name: reader}, {}).run(
            [HumanMessage(content="读取并回答")])
    trace_context.outcome(result)
    assert reads == ["read"] and budget.tool_calls == 1
    assert budget.model_calls == len(requests) == (2 if limit in {"none", "tool_calls"} else 1)
    if limit == "none":
        assert result.reply == "查到的结果是 42。" and phases[-1]["phase"] == "responded"
    else:
        assert result.execution["blocked"] == [{"reason": limit}]
        assert phases[-1]["phase"] == "blocked" and "上限" in result.reply
    assert all("正在整理" not in label for label in progress)


def test_capacity_inspection_cannot_poison_a_smaller_call_or_consume_budget():
    budget = RequestBudget(BudgetLimits(request_tokens=1000, context_tokens=1000))
    large = [HumanMessage(content="x" * 3000)]
    assert budget.call_block_reason(large, output_limit=10) == "context_tokens"
    assert budget.reason is None and budget.tokens == budget.model_calls == 0
    ticket = budget.reserve([HumanMessage(content="hi")], output_limit=10)
    budget.settle(ticket, AIMessage(content="ok", usage_metadata={
        "input_tokens": 4, "output_tokens": 2, "total_tokens": 6}))
    assert budget.tokens == 6 and budget.model_calls == 1


def test_exhausted_model_budget_cannot_pay_for_another_call_or_execute_it():
    reads = []

    def read_fixture() -> str:
        reads.append("read")
        return ok("fact")

    reader = StructuredTool.from_function(read_fixture, name="fixture_read", description="Read.")

    class Model:
        max_tokens = 64

        def bind_tools(self, *args, **kwargs):
            return self

        def invoke(self, prompt):
            return AIMessage(content="", tool_calls=[{
                "name": "fixture_read", "args": {}, "id": f"read-{len(reads)}"}])

    budget = RequestBudget(BudgetLimits(model_calls=1))
    with budget_scope(budget):
        session = ToolSession(Model(), {}, {reader.name: reader}, {})
        result = session.run([HumanMessage(content="读取")])
    assert reads == ["read"] and result.execution["blocked"] == [{"reason": "model_calls"}]
    assert "上限" in result.reply
    assert budget.model_calls == 1 and budget.tool_calls == 1


def test_context_exit_cannot_discard_facts_to_force_an_extra_model_call():
    calls = []

    def read_fixture() -> str:
        return ok("x" * 5000)

    reader = StructuredTool.from_function(read_fixture, name="fixture_read", description="Read.")

    class Model:
        max_tokens = 64

        def bind_tools(self, *args, **kwargs):
            return self

        def invoke(self, prompt):
            calls.append(list(prompt))
            return AIMessage(content="", tool_calls=[{
                "name": "fixture_read", "args": {}, "id": "read"}])

    budget = RequestBudget(BudgetLimits(context_tokens=1000))
    with budget_scope(budget):
        session = ToolSession(Model(), {}, {reader.name: reader}, {})
        result = session.run([HumanMessage(content="读取")])
    assert len(calls) == 1
    assert "上限" in result.reply and budget.reason == "context_tokens"
