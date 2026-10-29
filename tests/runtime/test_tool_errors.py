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
    model = Capture(*[AIMessageChunk(content="", tool_calls=[{
        "name": tool.name, "id": ident, "args": {"role": role}}])
        for ident, role in (("invalid", private), ("corrected", "zako"))],
        AIMessageChunk(content="完成。"))
    result = ToolSession(model, {}, {tool.name: tool}, {}).run([HumanMessage(content="执行夹具")])
    failure = next(m for m in model.prompts[1] if isinstance(m, ToolMessage))
    assert failure.status == "error" and called == ["zako"]
    fields = json.loads(failure.content)["fields"]
    assert fields[0]["field"] == "role"
    assert "user" in fields[0]["message"] and "zako" in fields[0]["message"]
    assert private not in failure.content
    assert result.reply == "完成。"
