"""The standard loop repairs unexecuted errors and reviews explicit pending work."""

import pytest
from _own_support import config
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.tools import tool
from langgraph.checkpoint.memory import InMemorySaver
from test_confirmation_continuation import Model, call

from agent.products.blog.application import BlogApplication
from agent.runtime.checkpoint_conversations import CheckpointConversationStore
from agent.runtime.results import turn_phase
from agent.runtime.service import AgentRuntime
from agent.runtime.session import ToolSession
from agent.runtime.task_planning import TaskPlanningMiddleware, tools
from agent.runtime.turns import AgentCancelled


def malformed():
    return AIMessage(content="", invalid_tool_calls=[{
        "name": "fixture_read", "args": "{", "id": "broken", "error": "JSON"}])


@pytest.mark.parametrize("first", [malformed(), AIMessage(content="")])
def test_unexecuted_protocol_error_is_corrected_without_user_resubmission(first):
    reads = []

    @tool
    def fixture_read() -> str:
        """Read one fixture fact."""
        reads.append(1)
        return "verified 42"

    model = Model(first, AIMessage(content="", tool_calls=[call("fixture_read", "valid")]),
                  AIMessage(content="结果是42。"))
    selected = ToolSession(model, config(), {fixture_read.name: fixture_read}, {})
    result = selected.run([HumanMessage(content="读取并回答")])
    assert result.reply == "结果是42。" and reads == [1]
    assert selected.framework_state["response_repairs"] == 1
    assert not any(getattr(m, "invalid_tool_calls", None) for m in model.prompts[1])


def test_repeated_malformed_output_stops_after_one_correction():
    model = Model(malformed(), malformed())
    selected = ToolSession(model, config(), {}, {})
    result = selected.run([HumanMessage(content="处理请求")])
    assert len(model.prompts) == 2 and selected.calls_used == 0
    assert turn_phase(result) == "blocked"
    assert result.execution["blocked"] == [{"reason": "invalid_tool_call"}]


def test_explicit_pending_work_continues_instead_of_ending_at_a_promise():
    pending = [{"content": "读取结果并交付", "status": "in_progress"}]
    model = Model(AIMessage(content="", tool_calls=[call("write_todos", "plan", todos=pending)]),
                  AIMessage(content="我会继续处理。"),
                  AIMessage(content="", tool_calls=[call("write_todos", "done", todos=[{
                      "content": pending[0]["content"], "status": "completed"}])]),
                  AIMessage(content="交付的解释。"))
    selected = ToolSession(model, config(), tools(), {}, middleware=(TaskPlanningMiddleware,))
    result = selected.run([HumanMessage(content="解释结果")])
    assert len(model.prompts) == 4 and result.reply.endswith("交付的解释。")
    assert selected.framework_state["completion_checks"] == 1
    assert turn_phase(result) == "responded"


def test_pending_work_is_explicitly_blocked_after_the_single_review():
    pending = [{"content": "尚未交付的比较", "status": "in_progress"}]
    model = Model(AIMessage(content="", tool_calls=[call("write_todos", "plan", todos=pending)]),
                  AIMessage(content="我会继续。"), AIMessage(content="暂时无法完成。"))
    selected = ToolSession(model, config(), tools(), {}, middleware=(TaskPlanningMiddleware,))
    result = selected.run([HumanMessage(content="比较两篇文章")])
    assert len(model.prompts) == 3 and turn_phase(result) == "blocked"
    assert result.execution["unfinished_work"] == pending
    assert "尚未交付的比较" in result.reply


def test_reconstruction_cannot_reset_the_protocol_correction_allowance():
    saver, cfg = InMemorySaver(), config()

    def runtime(model):
        return AgentRuntime(CheckpointConversationStore(saver), application=BlogApplication(),
                            model_factory=lambda **_: model)

    class StopAfterRepair(Model):
        def stream(self, prompt, **kwargs):
            if self.prompts:
                raise AgentCancelled()
            yield from super().stream(prompt, **kwargs)

    first = StopAfterRepair(malformed())
    with pytest.raises(AgentCancelled):
        list(runtime(first).stream_events({"messages": [HumanMessage(content="处理请求")],
                                          "task_id": "repair-task"}, cfg))
    resumed = Model(malformed())
    result = list(runtime(resumed).stream_events({"resume_task": "repair-task"}, cfg))[-1]
    assert len(resumed.prompts) == 1 and turn_phase(result) == "blocked"
