"""Discovery repairs inactive names without expanding the caller's authorized scope."""

import json

import pytest
from _memory_support import Capture
from langchain_core.messages import AIMessageChunk, HumanMessage, ToolMessage
from langchain_core.tools import tool

from agent.runtime.catalog import tool_owners
from agent.runtime.session import ToolSession


def call(name, ident):
    return AIMessageChunk(content="", tool_calls=[{"name": name, "args": {}, "id": ident}])


def test_inactive_tool_requires_explicit_discovery_before_execution():
    reads = []

    @tool
    def read_fixture() -> str:
        """Read an authorized synthetic record."""
        reads.append(True)
        return "fixture fact"

    model = Capture(call("read_fixture", "early"), call("load_fixture_info_tools", "load"),
                    call("load_fixture_info_tools", "again"), call("read_fixture", "read"),
                    AIMessageChunk(content="done"))
    session = ToolSession(model, {}, {}, {"fixture": {"read_fixture": read_fixture}})
    result = session.run([HumanMessage(content="read")])
    error = next(m for m in model.prompts[1] if isinstance(m, ToolMessage))
    assert error.status == "error" and json.loads(error.content) == {
        "error": "tool_not_loaded", "tool": "read_fixture",
        "discovery_tools": ["load_fixture_info_tools"]}
    assert result.reply == "done" and reads == [True]
    assert session.state["loaded"] == ["fixture"]


def test_unknown_tool_does_not_reveal_an_unauthorized_domain_or_execute():
    model = Capture(call("private_admin_write", "unknown"), AIMessageChunk(content="unavailable"))
    ToolSession(model, {}, {}, {"public": {}}).run([HumanMessage(content="read")])
    error = next(m for m in model.prompts[-1] if isinstance(m, ToolMessage))
    assert json.loads(error.content) == {"error": "unknown_tool", "tool": "private_admin_write",
                                         "discovery_tools": ["load_public_info_tools"]}


def test_shared_primitive_has_all_authorized_discovery_paths():
    @tool
    def read() -> str:
        """Read a shared primitive."""
        return "fact"

    owners = tool_owners({"read": read}, {"public": {"read": read}, "admin": {
        "read": read.model_copy()}})
    assert owners == {"read": ["load_public_info_tools", "load_admin_info_tools"]}


@pytest.mark.parametrize("invalid", ["conflict", "key", "initial_key", "reserved"])
def test_catalog_rejects_ambiguous_or_shadowed_declarations(invalid):
    @tool
    def read() -> str:
        """Read a synthetic primitive."""
        return "fact"

    initial, domains = {}, {"fixture": {"read": read}}
    if invalid == "conflict":
        initial["read"] = read.model_copy(update={"description": "Different capability"})
    elif invalid == "key":
        domains["fixture"] = {"different_name": read}
    elif invalid == "initial_key":
        initial["different_name"] = read
    else:
        shadow = read.model_copy(update={"name": "load_fixture_info_tools"})
        domains["fixture"][shadow.name] = shadow
    with pytest.raises(ValueError):
        tool_owners(initial, domains)
