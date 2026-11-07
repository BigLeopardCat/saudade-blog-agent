"""Protocol repair remains bounded; optional todo state never schedules extra model calls."""

import pytest
from _own_support import config
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.tools import tool
from langgraph.checkpoint.memory import InMemorySaver
from test_confirmation_continuation import Model, call

from agent.products.blog.application import BlogApplication
from agent.runtime.checkpoint_conversations import CheckpointConversationStore
from agent.runtime.results import ReplyReset, turn_phase
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


def test_deferred_checklist_does_not_force_continuation_or_duplicate_a_delivered_answer():
    pending = [{"content": "阅读文章22，再读文章23；等待用户后续指令", "status": "pending"}]
    model = Model(AIMessage(content="", tool_calls=[call("write_todos", "plan", todos=pending)]),
                  AIMessage(content="已记住阅读顺序，等你要求时再读。"))
    selected = ToolSession(model, config(), tools(), {}, middleware=(TaskPlanningMiddleware,))
    result = selected.run([HumanMessage(content="记住阅读顺序，现在不要读正文。")])
    assert len(model.prompts) == 2 and selected.calls_used == 1
    assert result.reply == "已记住阅读顺序，等你要求时再读。"
    assert selected.framework_state["todos"] == pending
    assert turn_phase(result) == "responded"
    assert not any(message.additional_kwargs.get("runtime_feedback")
                   for prompt in model.prompts for message in prompt)


def test_pending_checklist_is_not_a_completion_or_failure_verdict():
    pending = [{"content": "尚未交付的比较", "status": "in_progress"}]
    model = Model(AIMessage(content="", tool_calls=[call("write_todos", "plan", todos=pending)]),
                  AIMessage(content="目前无法完成比较，缺少第二篇原文。"))
    selected = ToolSession(model, config(), tools(), {}, middleware=(TaskPlanningMiddleware,))
    result = selected.run([HumanMessage(content="比较两篇文章")])
    assert len(model.prompts) == 2 and turn_phase(result) == "responded"
    assert selected.framework_state["todos"] == pending
    assert not (result.execution or {}).get("blocked")
    assert "尚未交付的比较" not in result.reply
    assert result.reply == "目前无法完成比较，缺少第二篇原文。"


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


def test_invalid_success_claim_is_withdrawn_from_stream_and_durable_reply():
    saver, cfg = InMemorySaver(), config()
    invalid = malformed().model_copy(update={"content": "本次操作已经成功完成。"})
    model = Model(invalid, AIMessage(content="本次操作尚未执行。"))
    selected = AgentRuntime(CheckpointConversationStore(saver), application=BlogApplication(),
                            model_factory=lambda **_: model)
    events = list(selected.stream_events({"messages": [HumanMessage(content="处理请求")],
                                          "task_id": "withdraw-prose"}, cfg))
    assert sum(isinstance(e, ReplyReset) for e in events) == 1
    result = events[-1]
    assert result.reply == "本次操作尚未执行。"
    assert "已经成功" not in str(selected.store.load(cfg).messages)
    assert "已经成功" not in str(model.prompts[-1])
