"""Typed tool failures stop an ordered batch and let the next model turn repair it."""

import pytest
from _own_support import config
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import tool
from langgraph.checkpoint.memory import InMemorySaver
from test_confirmation_continuation import Model, call

from agent.products.blog.application import BlogApplication
from agent.products.blog.capabilities import ToolCatalog
from agent.runtime.checkpoint_conversations import CheckpointConversationStore
from agent.runtime.service import AgentRuntime
from eval.own_fixtures import favorite_backend
from tools.result import ToolResult, ok


@pytest.mark.parametrize("kind", ["invalid_args", "unavailable", "not_found"])
def test_failed_prerequisite_does_not_prepare_the_next_operation_until_repaired(kind):
    checked = []

    @tool
    def check_input(content: str) -> str:
        """Validate a synthetic prerequisite."""
        checked.append(content)
        return ok("已核实") if content else ToolResult("请重新核实输入", kind)

    model = Model(
        AIMessage(content="", tool_calls=[call("load_favorites_info_tools", "load")]),
        AIMessage(content="", tool_calls=[call("check_input", "bad", content=""),
            call("prepare_favorite_change", "blocked", action="add", article_id=19)]),
        AIMessage(content="", tool_calls=[call("check_input", "repaired", content="已核实"),
            call("prepare_favorite_change", "prepared", action="add", article_id=19)]))
    runtime = AgentRuntime(CheckpointConversationStore(InMemorySaver()),
        application=BlogApplication(ToolCatalog(read_tools={check_input.name: check_input})),
        model_factory=lambda **_: model)
    with favorite_backend() as state:
        result = list(runtime.stream_events(
            {"messages": [HumanMessage(content="先核实输入，再准备收藏文章19。")]}, config()))[-1]
    errors = {message.tool_call_id: message for message in model.prompts[2]
              if isinstance(message, ToolMessage) and message.status == "error"}
    assert {"bad", "blocked"} <= errors.keys()
    assert checked == ["", "已核实"]
    assert result.execution["pending_confirm"] and not state["favorites"]
    assert len(state["requests"]) == 2 and all(r.method == "GET" for r in state["requests"])
