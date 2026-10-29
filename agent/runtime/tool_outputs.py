"""Project application outcomes onto standard tool content and artifacts."""

from functools import wraps

from langchain_core.tools import StructuredTool, ToolException
from pydantic import ValidationError

from agent.runtime.message_content import text_content
from agent.runtime.results import TurnResult
from agent.runtime.tool_errors import validation_result
from tools.result import ToolResult, invalid_args, unavailable
from utils.trace import record


def _output(result, response_format):
    if result is None:
        result = unavailable("无权读取此数据。")
    if response_format == "content_and_artifact" and isinstance(result, tuple):
        return result
    if isinstance(result, TurnResult):
        content = "操作已准备，等待用户决定。"
        return content, {
            "application_result": {"reply": result.reply, "execution": result.execution}}
    if isinstance(result, ToolResult):
        return str(result), {"application_data": {
            "kind": result.kind, "data": result.data, "meta": result.meta}}
    return result, None


def framework_tool(original):
    """Preserve the tool schema; let ToolNode own validation, injection and callbacks."""
    if not isinstance(original, StructuredTool):
        return original

    def failed(exc):
        record("native", "tool_failed", name=original.name, error=type(exc).__name__)
        return unavailable(f"{original.name} 调用失败（{type(exc).__name__}），请如实说明。")

    def invalid(error):
        record("native", "tool_failed", name=original.name, error="invalid_arguments")
        return str(validation_result(error))

    updates = {"response_format": "content_and_artifact", "handle_validation_error": invalid,
               "handle_tool_error": lambda error: str(invalid_args(str(error)))}
    if original.func is not None:
        @wraps(original.func)
        def invoke(*args, **kwargs):
            try:
                result = original.func(*args, **kwargs)
            except (ValidationError, ToolException):
                raise
            except ValueError as exc:
                result = failed(exc)
            return _output(result, original.response_format)

        updates["func"] = invoke
    if original.coroutine is not None:
        @wraps(original.coroutine)
        async def ainvoke(*args, **kwargs):
            try:
                result = await original.coroutine(*args, **kwargs)
            except (ValidationError, ToolException):
                raise
            except ValueError as exc:
                result = failed(exc)
            return _output(result, original.response_format)

        updates["coroutine"] = ainvoke
    return original.model_copy(update=updates)


def application_result(message):
    """Recover structured data without putting artifacts into model text."""
    artifact = message.artifact if isinstance(message.artifact, dict) else {}
    terminal = artifact.get("application_result")
    if terminal is not None:
        return TurnResult(terminal["reply"], terminal["execution"])
    data = artifact.get("application_data")
    if data is not None:
        return ToolResult(text_content(message.content), kind=data["kind"],
                          meta=data.get("meta"), data=data.get("data"))
    return message
