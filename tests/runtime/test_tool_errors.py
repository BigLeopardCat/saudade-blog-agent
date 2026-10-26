"""Schema failures identify the field and never expose rejected private input."""

import json
from typing import Literal

from langchain_core.tools import StructuredTool

from agent.runtime.session import ToolSession


def test_schema_validation_explains_role_field_without_calling_the_tool():
    called = []

    def operation(role: Literal["user", "zako"]) -> str:
        called.append(role)
        return "done"

    tool = StructuredTool.from_function(operation, description="Set role.")
    session = ToolSession(object(), {}, {tool.name: tool}, {})
    private = "PRIVATE_REJECTED_ARGUMENT"
    failure = session._invoke_tool(tool.name, {"role": private})
    assert failure.kind == "invalid_args" and not called
    fields = json.loads(failure)["fields"]
    assert fields[0]["field"] == "role"
    assert "user" in fields[0]["message"] and "zako" in fields[0]["message"]
    assert private not in str(failure)
    assert session._invoke_tool(tool.name, {"role": "zako"}) == "done"
    assert called == ["zako"]
