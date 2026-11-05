"""Appearance completion requires observed settings and resumes the same checkpoint."""

import json

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver
from pydantic import ValidationError

from agent.api.schemas import ChatRequest
from agent.contracts.events import StreamEvent
from agent.principal import SOURCE_ASSERTION, Principal
from agent.products.blog.application import BlogApplication
from agent.runtime.checkpoint_conversations import CheckpointConversationStore
from agent.runtime.service import AgentRuntime
from agent.transport import iter_transport_events


class Model:
    max_tokens = 128

    def __init__(self):
        self.prompts = []

    def bind_tools(self, *args, **kwargs):
        return self

    def invoke(self, messages, **kwargs):
        self.prompts.append(messages)
        if len(self.prompts) == 1:
            return AIMessage(content="", tool_calls=[{
                "id": "appearance", "name": "set_page_appearance",
                "args": {"dark_mode": "on", "rain": "off", "snow": "on"}}])
        return AIMessage(content="已收到显示设置的实际结果。")


@pytest.mark.parametrize("reported,expected", [
    ({"status": "completed", "settings": {"dark_mode": "on", "rain": "off", "snow": "on"}},
     "completed"),
    ({"status": "completed", "settings": {"dark_mode": "on", "rain": "on", "snow": "on"}},
     "failed"),
    ({"status": "completed"}, "failed"),
    ({"status": "timeout", "settings": {"dark_mode": "off"}}, "timeout"),
    ({"status": "failed", "settings": {"dark_mode": "on"}}, "failed"),
])
def test_appearance_waits_and_recovers_from_actual_settings(reported, expected):
    saver, model = InMemorySaver(), Model()
    cfg = {"configurable": {"thread_id": "conv_42", "conversation_id": 42, "user_id": 7,
        "principal": Principal(7, "user", SOURCE_ASSERTION), "browser_feedback": True}}

    def runtime():
        return AgentRuntime(
            CheckpointConversationStore(saver),
            application=BlogApplication(),
            model_factory=lambda **_: model,
        )

    state = {"messages": [HumanMessage(content="夜间模式，下雨换成下雪")],
             "task_id": "appearance-task"}
    paused = list(runtime().stream_events(state, cfg))[-1]
    command = paused.execution["client_action"]
    assert command["kind"] == "appearance" and len(model.prompts) == 1
    assert command["settings"] == {"dark_mode": "on", "rain": "off", "snow": "on"}
    assert runtime().get_state(cfg, subgraphs=True).interrupts
    assert next(iter(iter_transport_events([paused]))).name == "command"
    resumed_cfg = {"configurable": {**cfg["configurable"], "browser_result": {
        "command_id": command["command_id"], "path": "/", **reported}}}
    done = list(runtime().stream_events({"resume_task": "appearance-task"}, resumed_cfg))[-1]
    assert len(model.prompts) == 2 and not runtime().store.load(cfg).unfinished
    observations = [json.loads(m.content) for m in model.prompts[-1]
                    if isinstance(m, ToolMessage) and m.name == "set_page_appearance"]
    statuses = [m.status for m in model.prompts[-1]
                if isinstance(m, ToolMessage) and m.name == "set_page_appearance"]
    assert statuses[-1] == ("success" if expected == "completed" else "error")
    assert observations[-1]["status"] == expected
    assert observations[-1]["completion_confirmed"] is (expected == "completed")
    assert observations[-1]["observed"] == reported.get("settings")
    assert all(row["browser_status"] == expected for row in done.execution["receipts"])
    assert not any(
        (isinstance(frame, StreamEvent) and frame.name == "command")
        for frame in iter_transport_events([done])
    )


@pytest.mark.parametrize("settings", [{"snow": "toggle"}, {"permission": "on"}, {"rain": True}])
def test_browser_results_cannot_supply_unbounded_or_ambiguous_settings(settings):
    with pytest.raises(ValidationError):
        ChatRequest(message="", conversation_id=42, resume_task="task", browser_result={
            "command_id": "cmd", "status": "completed", "path": "/", "settings": settings})
