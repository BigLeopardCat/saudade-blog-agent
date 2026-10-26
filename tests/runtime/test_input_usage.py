"""Observed input usage only replaces estimates for the exact same request prefix."""

import json

import httpx
import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import StructuredTool

from agent.runtime.budget import BudgetLimits, RequestBudget, budget_scope
from agent.runtime.message_content import estimate_tokens
from agent.runtime.session import ToolSession
from models.llm import get_llm
from tools.result import invalid_args, ok


def anchor(budget, messages, menu, *, usage=1000):
    ticket = budget.reserve(messages, menu, output_limit=64, model_key=7)
    budget.settle(ticket, AIMessage(content="reply", usage_metadata={
        "input_tokens": usage, "output_tokens": 10, "total_tokens": usage + 10}))
    return ticket


def test_exact_prefix_uses_actual_input_and_estimates_only_appended_tool_exchange():
    budget = RequestBudget()
    messages = [SystemMessage(content="中文前缀" * 400), HumanMessage(content="处理")]
    menu = [{"type": "function", "function": {"name": "fixture", "description": "read"}}]
    anchor(budget, messages, menu)
    appended = [AIMessage(content="", tool_calls=[{
        "name": "fixture", "args": {"text": "参数"}, "id": "tool"}],
        additional_kwargs={"reasoning_content": "reasoning replay"}),
        ToolMessage(content="字段无效，请修正", tool_call_id="tool")]
    ticket = budget.reserve([*messages, *appended], menu, output_limit=64, model_key=7)
    assert ticket.estimated_input == 1000 + estimate_tokens(appended)
    assert budget.tokens == 1010 + ticket.estimated_input + 64


@pytest.mark.parametrize("change", ["system", "history", "tools", "model", "unknown_usage"])
def test_any_prefix_or_envelope_change_falls_back_to_complete_framework_estimate(change):
    budget, fresh = RequestBudget(), RequestBudget()
    messages = [SystemMessage(content="前缀" * 400), HumanMessage(content="request")]
    menu = [{"type": "function", "function": {"name": "fixture", "description": "read"}}]
    anchor(budget, messages, menu, usage=0 if change == "unknown_usage" else 1000)
    current = list(messages)
    if change == "system":
        current[0] = SystemMessage(content="不同前缀" * 400)
    if change == "history":
        current[1] = HumanMessage(content="changed history")
    if change == "tools":
        menu = [*menu, {"type": "function", "function": {"name": "new", "description": "read"}}]
    model = 8 if change == "model" else 7
    expected = fresh.reserve(current, menu, output_limit=64).estimated_input
    ticket = budget.reserve(current, menu, output_limit=64, model_key=model)
    assert ticket.estimated_input == expected


def test_real_sdk_can_correct_tool_arguments_without_premature_schema_removal():
    requests, calls = [], []

    def operation(content: str) -> str:
        calls.append(content)
        return invalid_args("content为空，请填写正文") if not content else ok("已取得正确内容")

    tool = StructuredTool.from_function(operation, name="fixture_operation",
                                         description="x" * 9000)

    def provider(request):
        body = json.loads(request.content)
        requests.append(body)
        assert body["tools"] and body["tool_choice"] == "auto"
        message = {"role": "assistant", "content": "完成"}
        if len(requests) <= 2:
            message.update(content="", tool_calls=[{"id": "call-" + str(len(requests)),
                "type": "function", "function": {"name": tool.name,
                    "arguments": json.dumps({"content": "" if len(requests) == 1 else "正文"})}}])
        return httpx.Response(200, json={"id": "fixture", "model": "fixture",
            "object": "chat.completion", "choices": [{"index": 0, "message": message,
                "finish_reason": "tool_calls" if len(requests) <= 2 else "stop"}],
            "usage": {"prompt_tokens": 1000, "completion_tokens": 10, "total_tokens": 1010}})

    budget = RequestBudget(BudgetLimits(request_tokens=4000))
    with httpx.Client(transport=httpx.MockTransport(provider)) as client, budget_scope(budget):
        model = get_llm(api_key="test-only", base_url="https://model.test/v1", model="fixture",
                        http_client=client, max_tokens=64, streaming=False, enable_thinking=False)
        session = ToolSession(model, {}, {tool.name: tool}, {})
        result = session.run([SystemMessage(content="x" * 2000), HumanMessage(content="execute")])
    assert result.reply == "完成" and calls == ["", "正文"] and len(requests) == 3
    assert budget.tokens == 3030 and not session.finalizing
    assert requests[0]["tools"] == requests[1]["tools"] == requests[2]["tools"]
