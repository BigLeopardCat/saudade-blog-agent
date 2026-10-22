"""Recoverable tool protocol errors stay inside the bounded native loop."""

import json

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from agent.runtime.session import ToolSession


def call(name):
    return AIMessage(content="", tool_calls=[{"name": name, "args": {}, "id": name}])


class Model:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.prompts = []

    def bind_tools(self, *args, **kwargs):
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


@pytest.mark.parametrize("invalid,reason", [("invented", "unknown_tool"),
                                          ("load_site_info_tools", "domain_already_loaded")])
def test_tool_error_provides_current_menu_without_repeating_io(invalid, reason):
    read = Read()
    model = Model([call("load_site_info_tools"), call(invalid), call(read.name),
                   AIMessage(content="作者资料")])
    session = ToolSession(model, {}, {}, {"site": {read.name: read}})
    result = session.run([HumanMessage(content="查询作者")])
    assert result.reply == "作者资料" and read.calls == 1
    error = model.prompts[2][-1]
    assert error.status == "error"
    data = json.loads(error.content)
    assert data["error"] == reason
    assert data["available_tools"] == ["delegate_to_site_agent", read.name]


def test_repeated_invalid_requests_exhaust_the_existing_round_budget():
    model = Model([call("invented") for _ in range(5)])
    read = Read()
    session = ToolSession(model, {}, {read.name: read}, {})
    assert session.run([HumanMessage(content="查询作者")]) is None
    assert len(model.prompts) == 5 and read.calls == 0


@pytest.mark.parametrize("bad", [None, {"name": []}, {"name": "x", "id": "id", "args": None}])
def test_invalid_wire_values_are_rejected_before_menu_lookup(bad):
    session = ToolSession(Model([]), {}, {}, {})
    assert session._accept_calls([bad], 0) == "invalid_tool_call"
