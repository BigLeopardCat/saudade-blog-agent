"""Dynamic tools use framework injection, callbacks and standard state updates."""

from _memory_support import Capture
from langchain.tools import ToolRuntime
from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.messages import AIMessageChunk, HumanMessage, ToolMessage
from langchain_core.tools import ToolException, tool
from langchain_core.utils.function_calling import convert_to_openai_tool
from langgraph.types import Command

from agent.runtime.session import ToolSession
from agent.runtime.tool_outputs import framework_tool
from tools.result import ok


def call(name, ident, **args):
    return AIMessageChunk(content="", tool_calls=[{"name": name, "args": args, "id": ident}])


def test_dynamic_tool_receives_trusted_framework_runtime_and_callbacks():
    events, observed = [], []

    class Callbacks(BaseCallbackHandler):
        def on_tool_start(self, serialized, input_str, **kwargs):
            events.append("start")

        def on_tool_end(self, output, **kwargs):
            events.append("end")
            assert isinstance(output, ToolMessage) and output.tool_call_id == "read"

    @tool
    def dynamic_read(runtime: ToolRuntime) -> object:
        """Read a synthetic value using framework execution context."""
        observed.append(runtime.tool_call_id)
        assert runtime.state["messages"][-1].tool_calls[0]["id"] == "read"
        return ok("fixture fact", data={"value": 7})

    model = Capture(call("load_fixture_info_tools", "load"),
                    call(dynamic_read.name, "read", runtime="UNTRUSTED_OVERRIDE"),
                    AIMessageChunk(content="done"))
    result = ToolSession(model, {"callbacks": [Callbacks()]}, {}, {
        "fixture": {dynamic_read.name: dynamic_read}}).run([HumanMessage(content="read")])
    assert result.reply == "done" and observed == ["read"] and events == ["start", "end"]
    message = next(m for m in model.prompts[-1] if isinstance(m, ToolMessage)
                   and m.tool_call_id == "read")
    assert message.content == "fixture fact" and message.artifact == {"kind": "ok"}
    assert dynamic_read.response_format == "content"
    assert convert_to_openai_tool(dynamic_read) == convert_to_openai_tool(
        framework_tool(dynamic_read))


def test_framework_command_state_is_visible_to_the_next_tool_round():
    @tool
    def update(runtime: ToolRuntime) -> Command:
        """Save synthetic structured response in framework state."""
        return Command(update={"structured_response": {"value": 7}, "messages": [
            ToolMessage(content="stored", tool_call_id=runtime.tool_call_id)]})

    seen = []

    @tool
    def read(runtime: ToolRuntime) -> str:
        """Read a synthetic structured value from framework state."""
        seen.append(runtime.state["structured_response"])
        return "read"

    model = Capture(call("update", "save"), call("read", "read"), AIMessageChunk(content="done"))
    result = ToolSession(model, {}, {t.name: t for t in (update, read)}, {}).run([
        HumanMessage(content="update and read")])
    assert result.reply == "done" and seen == [{"value": 7}]
    messages = [m for m in model.prompts[-1] if isinstance(m, ToolMessage)]
    assert [m.tool_call_id for m in messages] == ["save", "read"]


def test_expected_tool_exception_is_a_recoverable_framework_error_message():
    @tool
    def failure() -> str:
        """Reject a synthetic invalid operation."""
        raise ToolException("Specify an existing fixture ID")

    model = Capture(call(failure.name, "failure"), AIMessageChunk(content="请提供编号"))
    result = ToolSession(model, {}, {failure.name: failure}, {}).run([HumanMessage(content="read")])
    message = next(m for m in model.prompts[-1] if isinstance(m, ToolMessage))
    assert result.reply == "请提供编号"
    assert message.status == "error" and "fixture ID" in message.content


def test_output_adapter_preserves_both_sync_and_async_tool_contracts():
    import asyncio

    from langchain_core.tools import StructuredTool

    def sync_read(value: int) -> object:
        return ok("sync fact", data={"value": value})

    async def async_read(value: int) -> object:
        return ok("async fact", data={"value": value})

    original = StructuredTool.from_function(sync_read, coroutine=async_read,
                                            description="Read a synthetic value.")
    adapted = framework_tool(original)
    request = {"name": adapted.name, "args": {"value": 7}, "id": "read", "type": "tool_call"}
    sync_result = adapted.invoke(request)
    async_result = asyncio.run(adapted.ainvoke(request))
    assert sync_result.content == "sync fact" and async_result.content == "async fact"
    assert sync_result.artifact == async_result.artifact == {
        "application_data": {"kind": "ok", "data": {"value": 7}, "meta": {}}}
    assert original.response_format == "content"
