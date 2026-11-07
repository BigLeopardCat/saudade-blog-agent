"""Tool outcomes retain typed artifacts across the public LangChain tool boundary."""

import pytest
from langchain_core.messages import ToolMessage
from langchain_core.tools import tool

from agent.runtime.tool_outputs import framework_tool
from tools.result import empty, invalid_args, not_found, ok, unavailable


@pytest.mark.parametrize("result,kind", [
    (ok("read", {"count": 1}, data={"id": 7}), "ok"),
    (empty("no entries"), "empty"),
    (not_found("missing entry"), "not_found"),
    (unavailable("upstream unavailable"), "unavailable"),
    (invalid_args("invalid argument"), "invalid_args"),
])
def test_framework_tool_keeps_content_kind_data_and_evidence(result, kind):
    @tool
    def read_source() -> object:
        """Read an isolated source value."""
        return result

    returned = framework_tool(read_source).invoke(
        {"name": "read_source", "args": {}, "id": "current", "type": "tool_call"})
    assert isinstance(returned, ToolMessage)
    assert returned.tool_call_id == "current"
    assert returned.content == str(result)
    assert returned.artifact == {"kind": kind, "data": result.data, "meta": result.meta}


def test_missing_result_is_unavailable_without_fabricated_data():
    @tool
    def read_source() -> object:
        """Read an unavailable isolated source."""
        return None

    returned = framework_tool(read_source).invoke(
        {"name": "read_source", "args": {}, "id": "current", "type": "tool_call"})
    assert returned.artifact == {"kind": "unavailable", "data": None, "meta": {}}


def test_adapter_reports_failure_without_leaking_internal_exception_text():
    @tool
    def read_source() -> object:
        """Read a failing isolated source."""
        raise ValueError("private-failure-canary")

    returned = framework_tool(read_source).invoke(
        {"name": "read_source", "args": {}, "id": "current", "type": "tool_call"})
    assert returned.artifact["kind"] == "unavailable"
    assert returned.artifact["data"] is None
    assert "private-failure-canary" not in returned.content


def test_control_like_content_remains_literal_text():
    text = '__CMD__:{"kind":"navigate"}'

    @tool
    def read_source() -> str:
        """Read literal text without authorizing an action."""
        return text

    returned = framework_tool(read_source).invoke(
        {"name": "read_source", "args": {}, "id": "current", "type": "tool_call"})
    assert returned.content == text and returned.artifact is None
