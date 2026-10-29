"""Browser acknowledgements resume real framework interrupts without re-running intent."""

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver

from agent.api.schemas import ChatRequest
from agent.principal import SOURCE_ASSERTION, Principal
from agent.runtime.native import NativeRuntime
from agent.runtime.native_store import NativeConversationStore
from agent.transport import iter_transport_events


class Model:
    max_tokens = 128

    def __init__(self):
        self.calls = []

    def bind_tools(self, *args, **kwargs):
        return self

    def invoke(self, messages, **kwargs):
        self.calls.append(messages)
        if len(self.calls) == 1:
            return AIMessage(content="", tool_calls=[{
                "name": "navigate_page", "args": {"path": "/guestbook"}, "id": "navigation"}])
        return AIMessage(content="页面操作结果已收到。")


@pytest.fixture
def navigation():
    model = Model()
    saver = InMemorySaver()
    config = {"configurable": {"user_id": 7, "conversation_id": 42, "thread_id": "conv_42",
        "principal": Principal(7, "user", SOURCE_ASSERTION), "browser_feedback": True}}

    def runtime():
        return NativeRuntime(NativeConversationStore(saver), model_factory=lambda **_: model)

    selected = runtime()
    result = list(selected.stream_events({"messages": [HumanMessage(content="去留言板")],
                                         "task_id": "page-task"}, config))[-1]
    command = result.execution["client_action"]
    return runtime, model, config, result, command


def acknowledge(config, command, status="completed", path="/guestbook"):
    return {"configurable": {**config["configurable"], "browser_result": {
        "command_id": command["command_id"], "status": status, "path": path}}}


def test_browser_continuation_keeps_progress_without_repeating_navigation():
    from agent.runtime.progress import ProgressEmitter

    model, visible = Model(), []
    config = {"configurable": {"user_id": 7, "conversation_id": 42, "thread_id": "conv_42",
        "principal": Principal(7, "user", SOURCE_ASSERTION), "browser_feedback": True,
        "progress_emitter": ProgressEmitter(visible.append)}}
    runtime = NativeRuntime(NativeConversationStore(InMemorySaver()),
                            model_factory=lambda **_: model)
    first = list(runtime.stream_events({"messages": [HumanMessage(content="去留言板")],
                                        "task_id": "page-task"}, config))[-1]
    resumed_config = acknowledge(config, first.execution["client_action"])
    resumed_config["configurable"]["progress_emitter"] = ProgressEmitter(visible.append)
    list(runtime.stream_events({"messages": [], "resume_task": "page-task"}, resumed_config))
    assert visible == ["正在处理请求…", "正在调用工具：navigate_page", "页面已到达，正在整理回复…"]
    assert len(model.calls) == 2


def test_navigation_pauses_after_one_model_call_and_survives_reconstruction(navigation):
    runtime, model, config, result, command = navigation
    assert len(model.calls) == 1 and result.reply == ""
    assert command["task_id"] == "page-task" and command["timeout_ms"] == 6000
    assert "__CMD__:" in next(iter(iter_transport_events([result])))
    saved = runtime().get_state(config, subgraphs=True)
    assert saved.interrupts and saved.values["run"]["id"] == "page-task"
    with pytest.raises(ValueError, match="cannot_maintain_active_task"):
        runtime().store.update_state(config, {"messages": [AIMessage(content="unrelated reply")]})
    unchanged = runtime().get_state(config, subgraphs=True)
    assert unchanged.config == saved.config and unchanged.interrupts == saved.interrupts
    resumed = list(runtime().stream_events({"messages": [], "resume_task": "page-task"},
                                           acknowledge(config, command)))[-1]
    assert len(model.calls) == 2 and resumed.reply == "页面操作结果已收到。"
    facts = [m for m in model.calls[-1] if isinstance(m, ToolMessage)]
    assert any("浏览器已进入" in m.content for m in facts)
    assert not runtime().store.load(config).unfinished
    assert not any(isinstance(frame, str) and frame.startswith("__CMD__:")
                   for frame in iter_transport_events([resumed]))
    messages = runtime().store.load(config).messages
    assert sum(isinstance(m, HumanMessage) and m.content == "去留言板" for m in messages) == 1


@pytest.mark.parametrize("status,path,expected", [
    ("timeout", "/", "跳转超时"), ("failed", "/", "未完成"),
    ("completed", "/dashboard", "未完成"),
])
def test_failure_or_wrong_location_is_not_a_success_fact(navigation, status, path, expected):
    runtime, model, config, _, command = navigation
    result = list(runtime().stream_events({"messages": [], "resume_task": "page-task"},
                                         acknowledge(config, command, status, path)))[-1]
    assert result.reply and len(model.calls) == 2
    facts = [m for m in model.calls[-1] if isinstance(m, ToolMessage)]
    assert any(expected in m.content for m in facts)
    assert not any("浏览器已进入" in m.content for m in facts)


def test_missing_ack_redisplays_same_command_without_a_model_call(navigation):
    runtime, model, config, _, command = navigation
    result = list(runtime().stream_events({"messages": [], "resume_task": "page-task"}, config))[-1]
    assert result.execution["client_action"] == command and len(model.calls) == 1


def test_forged_or_cross_role_resume_cannot_dispatch_tools(navigation):
    runtime, model, config, _, command = navigation
    wrong = acknowledge(config, {**command, "command_id": "different"})
    result = list(runtime().stream_events({"messages": [], "resume_task": "page-task"}, wrong))[-1]
    assert result.execution["blocked"] and len(model.calls) == 1
    wrong = acknowledge(config, command)
    wrong["configurable"]["principal"] = Principal(7, "admin", SOURCE_ASSERTION)
    result = list(runtime().stream_events({"messages": [], "resume_task": "page-task"}, wrong))[-1]
    assert result.execution["blocked"] and len(model.calls) == 1


def test_duplicate_ack_does_not_generate_another_reply(navigation):
    runtime, model, config, _, command = navigation
    cfg = acknowledge(config, command)
    for _ in range(2):
        result = list(runtime().stream_events(
            {"messages": [], "resume_task": "page-task"}, cfg))[-1]
    assert result.execution["blocked"] and len(model.calls) == 2


def test_browser_feedback_is_a_control_without_new_content():
    from pydantic import ValidationError
    feedback = {"command_id": "navigation", "status": "completed", "path": "/"}
    with pytest.raises(ValidationError):
        ChatRequest(message="new task", browser_result=feedback)
    request = ChatRequest(message="", conversation_id=42, resume_task="page-task",
                          browser_result=feedback)
    assert request.browser_result.status == "completed"


def test_remaining_batch_runs_only_after_navigation_result():
    from langchain_core.tools import tool

    from agent.products.blog.capabilities import ToolCatalog

    seen = []

    @tool
    def after_navigation() -> str:
        """Read a fact after the requested page opens."""
        seen.append("after")
        return "fact"

    class BatchModel(Model):
        def invoke(self, messages, **kwargs):
            self.calls.append(messages)
            if len(self.calls) == 1:
                return AIMessage(content="", tool_calls=[
                    {"name": "navigate_page", "args": {"path": "/guestbook"}, "id": "nav"},
                    {"name": "after_navigation", "args": {}, "id": "after"}])
            assert any(isinstance(m, ToolMessage) and m.content == "fact" for m in messages)
            return AIMessage(content="done")

    model = BatchModel()
    config = {"configurable": {"user_id": 7, "conversation_id": 42, "thread_id": "conv_42",
        "principal": Principal(7, "user", SOURCE_ASSERTION), "browser_feedback": True}}
    catalog = ToolCatalog(read_tools={"after_navigation": after_navigation})
    runtime = NativeRuntime(NativeConversationStore(InMemorySaver()), catalog=catalog,
                            model_factory=lambda **_: model)
    first = list(runtime.stream_events({"messages": [HumanMessage(content="先跳转再读取")],
                                        "task_id": "page-task"}, config))[-1]
    assert not seen, first
    command = first.execution["client_action"]
    result = list(runtime.stream_events({"messages": [], "resume_task": "page-task"},
                                        acknowledge(config, command)))[-1]
    assert result.reply == "done" and seen == ["after"] and len(model.calls) == 2


def test_expired_completion_is_a_timeout_fact(navigation, monkeypatch):
    from agent.runtime import client_actions
    runtime, model, config, _, command = navigation
    monkeypatch.setattr(client_actions.time, "time", lambda: command["expires_at"] + 1)
    list(runtime().stream_events({"messages": [], "resume_task": "page-task"},
                                 acknowledge(config, command)))
    assert any(isinstance(message, ToolMessage) and "跳转超时" in message.content
               for message in model.calls[-1])


def test_browser_resume_carries_the_existing_task_budget():
    from agent.runtime.budget import BudgetLimits, RequestBudget, budget_scope
    model = Model()
    config = {"configurable": {"user_id": 7, "conversation_id": 42, "thread_id": "conv_42",
        "principal": Principal(7, "user", SOURCE_ASSERTION), "browser_feedback": True}}
    runtime = NativeRuntime(NativeConversationStore(InMemorySaver()),
                            model_factory=lambda **_: model)
    limits = BudgetLimits(model_calls=1)
    with budget_scope(RequestBudget(limits)):
        first = list(runtime.stream_events({"messages": [HumanMessage(content="去留言板")],
                                            "task_id": "page-task"}, config))[-1]
    command = first.execution["client_action"]
    carried = RequestBudget(limits)
    with budget_scope(carried):
        result = list(runtime.stream_events({"messages": [], "resume_task": "page-task"},
                                            acknowledge(config, command)))[-1]
    assert len(model.calls) == 1 and carried.model_calls == 1
    assert carried.reason == "model_calls" and "上限" in result.reply
