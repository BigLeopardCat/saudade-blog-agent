"""Framework validation explains fields without exposing rejected private input."""

import json
from typing import Literal

from _memory_support import Capture
from langchain_core.messages import AIMessageChunk, HumanMessage, ToolMessage
from langchain_core.tools import StructuredTool

from agent.runtime.session import ToolSession


def test_framework_validation_allows_a_corrected_call_without_echoing_private_input():
    called = []

    def operation(role: Literal["user", "zako"]) -> str:
        called.append(role)
        return "done"

    tool = StructuredTool.from_function(operation, description="Set role.")
    private = "PRIVATE_REJECTED_ARGUMENT"
    model = Capture(
        *[
            AIMessageChunk(
                content="", tool_calls=[{"name": tool.name, "id": ident, "args": {"role": role}}]
            )
            for ident, role in (("invalid", private), ("corrected", "zako"))
        ],
        AIMessageChunk(content="完成。"),
    )
    result = ToolSession(model, {}, {tool.name: tool}, {}).run([HumanMessage(content="执行夹具")])
    failure = next(m for m in model.prompts[1] if isinstance(m, ToolMessage))
    assert failure.status == "error" and called == ["zako"]
    fields = json.loads(failure.content)["fields"]
    assert fields[0]["field"] == "role"
    assert "user" in fields[0]["message"] and "zako" in fields[0]["message"]
    assert private not in failure.content
    assert result.reply == "完成。"


def test_missing_host_authority_is_a_tool_failure_that_can_be_explained_without_leaking_secrets():
    private = "PRIVATE_AUTH_CONTEXT"

    def operation() -> str:
        raise PermissionError(private)

    tool = StructuredTool.from_function(operation, description="Read a protected source.")
    model = Capture(
        AIMessageChunk(content="", tool_calls=[{"name": tool.name, "id": "read", "args": {}}]),
        AIMessageChunk(content="当前访问权限不可用，无法核实结果。"),
    )
    result = ToolSession(model, {}, {tool.name: tool}, {}).run([HumanMessage(content="查询")])
    failure = next(m for m in model.prompts[1] if isinstance(m, ToolMessage))
    assert failure.artifact["kind"] == "unavailable"
    assert "PermissionError" in failure.content and private not in failure.content
    assert result.reply == "当前访问权限不可用，无法核实结果。"
