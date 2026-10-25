"""Recoverable tool protocol errors stay inside the bounded native loop."""

import json

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from agent.runtime.budget import BudgetLimits, RequestBudget, budget_scope
from agent.runtime.session import ToolSession


def call(name):
    return AIMessage(content="", tool_calls=[{"name": name, "args": {}, "id": name}])


class Model:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.prompts = []

    def bind_tools(self, tools, **kwargs):
        from _budget_model import schemas
        self.kwargs = {"tools": schemas(tools)}
        return self

    def invoke(self, prompt):
        self.prompts.append(list(prompt))
        return next(self.responses)


class Read:
    name = "get_blog_info"

    def __init__(self):
        self.calls = 0

    def invoke(self, args, config=None):
        self.calls += 1
        return "作者资料"


def test_tool_error_provides_current_menu_without_repeating_io():
    read = Read()
    model = Model([call("load_site_info_tools"), call("invented"), call(read.name),
                   AIMessage(content="作者资料")])
    session = ToolSession(model, {}, {}, {"site": {read.name: read}})
    result = session.run([HumanMessage(content="查询作者")])
    assert result.reply == "作者资料" and read.calls == 1
    error = model.prompts[2][-1]
    assert error.status == "error"
    data = json.loads(error.content)
    assert data["error"] == "unknown_tool"
    assert data["available_tools"] == [read.name, "load_site_info_tools"]
    assert set(data["available_tools"]) == {
        item["function"]["name"] for item in model.kwargs["tools"]}


def test_repeated_domain_loading_still_exhausts_the_tool_budget_without_business_io():
    read = Read()
    model = Model([call("load_site_info_tools") for _ in range(3)])
    session = ToolSession(model, {}, {}, {"site": {read.name: read}})
    session.budget = RequestBudget(BudgetLimits(tool_calls=2))
    assert "已停止继续处理" in session.run([HumanMessage(content="查询作者")]).reply
    assert len(model.prompts) == 3 and read.calls == 0
    assert session.budget.tool_calls == 2
    replies = [next(item for item in reversed(prompt) if isinstance(item, ToolMessage))
               for prompt in model.prompts[1:]]
    assert all(reply.status == "success" for reply in replies)


def test_repeated_invalid_requests_exhaust_the_configured_model_budget():
    model = Model([call("invented") for _ in range(5)])
    read = Read()
    session = ToolSession(model, {}, {read.name: read}, {})
    session.budget = RequestBudget(BudgetLimits(model_calls=5))
    assert "已停止继续处理" in session.run([HumanMessage(content="查询作者")]).reply
    assert len(model.prompts) == 5 and read.calls == 0


@pytest.mark.parametrize("bad", [None, {"name": []}, {"name": "x", "id": "id", "args": None}])
def test_invalid_wire_values_are_rejected_before_menu_lookup(bad):
    session = ToolSession(Model([]), {}, {}, {})
    assert session._accept_calls([bad], 0) == "invalid_tool_call"


def test_loading_and_reading_in_one_batch_cannot_bypass_the_read_limit():
    read = Read()
    calls = [{"name": "load_site_info_tools", "args": {}, "id": "load"}]
    calls += [{"name": read.name, "args": {}, "id": f"read_{i}"} for i in range(5)]
    model = Model([AIMessage(content="", tool_calls=calls)])
    session = ToolSession(model, {}, {}, {"site": {read.name: read}})
    session.budget = RequestBudget(BudgetLimits(tool_calls=5))
    assert "已停止继续处理" in session.run([HumanMessage(content="查询作者")]).reply
    assert read.calls == 0


def test_exhausted_result_budget_stops_before_another_read():
    class LargeRead(Read):
        def invoke(self, args, config=None):
            self.calls += 1
            return "x" * 10000

    read = LargeRead()
    calls = [{"name": read.name, "args": {}, "id": f"read_{i}"} for i in range(3)]
    model = Model([AIMessage(content="", tool_calls=calls)])
    session = ToolSession(model, {}, {read.name: read}, {})
    assert "已停止继续处理" in session.run([HumanMessage(content="查询作者")]).reply
    assert read.calls == 2


def test_budget_stop_does_not_restart_another_runtime():
    model = Model([call("invented") for _ in range(5)])
    with budget_scope(RequestBudget(BudgetLimits(model_calls=5))):
        session = ToolSession(model, {}, {}, {})
        result = session.run([HumanMessage(content="查询作者")])
    assert "已停止继续处理" in result.reply
    assert len(model.prompts) == 5


def test_unexpected_tool_failure_stops_later_business_calls_in_the_same_batch():
    class BrokenRead(Read):
        def invoke(self, args, config=None):
            self.calls += 1
            raise RuntimeError("fixture failure")

    read = BrokenRead()
    calls = [{"name": read.name, "args": {}, "id": f"read-{index}"} for index in range(3)]
    session = ToolSession(Model([AIMessage(content="", tool_calls=calls)]), {},
                          {read.name: read}, {})
    with pytest.raises(RuntimeError, match="fixture failure"):
        session.run([HumanMessage(content="查询")])
    assert read.calls == 1
