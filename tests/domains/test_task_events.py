"""Background reports are durable native exchanges in their originating conversation."""

import hashlib
import secrets
from types import SimpleNamespace

import httpx
import pytest
from _own_support import config
from langchain_core.language_models.fake_chat_models import FakeListChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver

from agent import confirm
from agent.api.schemas import ChatRequest
from agent.api.turns import runtime_events
from agent.principal import SOURCE_ASSERTION, Principal
from agent.products.blog.application import BlogApplication
from agent.runtime.checkpoint_conversations import CheckpointConversationStore
from agent.runtime.service import AgentRuntime
from tools import base

RUN = "saudade_7_sc_" + "a" * 32
SCHEDULE = "sc_" + "a" * 32
VERSION = "c" * 64
EVENT_ID = "task_" + hashlib.sha256(f"{RUN}:{VERSION}".encode()).hexdigest()


class Model(FakeListChatModel):
    def bind_tools(self, tools, **kwargs):
        assert kwargs["tool_choice"] == "none"
        return self.bind(**kwargs)


@pytest.fixture
def scenario(monkeypatch):
    monkeypatch.setattr(confirm.settings, "confirmation_secret", secrets.token_urlsafe(32))
    row = dict(runId=RUN, scheduleId=SCHEDULE, eventVersion=VERSION, conversationId=42)
    outcome = dict(state="success", result={"status": "completed", "text": "已核对执行结果。"})
    requests = []

    def host(request):
        requests.append(request)
        assert request.method == "GET"
        assert request.url.params["conversation_id"] == "42"
        return httpx.Response(200, json={"code": 200, "data": row})

    def result(actor, schedule, run):
        assert (actor.uid, schedule, run) == (7, SCHEDULE, RUN)
        return outcome

    with httpx.Client(transport=httpx.MockTransport(host)) as client:
        monkeypatch.setattr(base, "_client", client)
        model = Model(responses=["定时任务已完成喵。"])
        app = BlogApplication(scheduling=SimpleNamespace(result=result))
        runtime = AgentRuntime(
            CheckpointConversationStore(InMemorySaver()),
            application=app,
            model_factory=lambda **_: model,
        )
        cfg = config(principal=Principal(7, "admin", SOURCE_ASSERTION))
        cfg["configurable"]["turn_id"] = EVENT_ID
        runtime.store.update_state(
            cfg,
            {
                "messages": [
                    HumanMessage(
                        content="完成后在这里告诉我。",
                        id="user:original",
                        additional_kwargs={"memory_actor": {"uid": 7, "role": "admin"}},
                    )
                ]
            },
        )
        event = {
            "turn_id": EVENT_ID,
            "task_id": EVENT_ID,
            "system_event": {"kind": "task_result", "run_id": RUN, "version": VERSION},
        }
        yield runtime, cfg, event, row, outcome, requests


def test_report_writes_native_tool_and_assistant_to_original_memory_and_replays(scenario):
    runtime, cfg, event, _, _, requests = scenario
    first = list(runtime.stream_events(event, cfg))[-1]
    assert first.reply == "定时任务已完成喵。"
    saved = runtime.store.load(cfg).messages
    assert [m.content for m in saved if isinstance(m, HumanMessage)] == ["完成后在这里告诉我。"]
    assert any(isinstance(m, ToolMessage) and "已核对执行结果" in m.content for m in saved)
    assert any(isinstance(m, AIMessage) and m.id == "result:" + EVENT_ID for m in saved)
    second = list(runtime.stream_events(event, cfg))[-1]
    assert second.reply == first.reply and len(requests) == 1
    assert runtime.store.load(cfg).messages == saved


@pytest.mark.parametrize("change", ["conversation", "version", "run", "role", "not_committed"])
def test_untrusted_or_unfinished_completion_never_generates_a_report(scenario, change):
    runtime, cfg, event, row, outcome, _ = scenario
    if change == "conversation":
        row["conversationId"] = 43
    if change == "version":
        row["eventVersion"] = "d" * 64
    if change == "run":
        row["runId"] = "another_run"
    if change == "role":
        cfg["configurable"]["principal"] = Principal(7, "user", SOURCE_ASSERTION)
    if change == "not_committed":
        outcome["state"] = "pending"
    before = runtime.store.load(cfg).messages
    with pytest.raises(ValueError, match="task_event_"):
        list(runtime.stream_events(event, cfg))
    assert runtime.store.load(cfg).messages == before


def test_task_event_input_is_typed_and_cannot_mix_content_or_approval():
    payload = dict(message="", conversation_id=42, task_event={"run_id": RUN, "version": VERSION})
    assert ChatRequest(**payload).task_event.run_id == RUN
    for update in [
        dict(message="write"),
        dict(confirm_token="token"),
        dict(resume_task="paused"),
        dict(image_event={"job_id": "11111111-1111-4111-8111-111111111111", "version": VERSION}),
        dict(task_event={"run_id": RUN, "version": VERSION, "status": "completed"}),
    ]:
        with pytest.raises(ValueError):
            ChatRequest(**{**payload, **update})
    captured = []
    runtime = SimpleNamespace(stream_events=lambda state, cfg: captured.append(state) or iter([]))
    list(
        runtime_events(
            runtime,
            [],
            thread_id="test",
            user_id=7,
            principal=None,
            conversation_id=42,
            task_event=payload["task_event"],
        )
    )
    assert captured[0]["system_event"] == {"kind": "task_result", **payload["task_event"]}


def test_completion_does_not_resume_or_replace_a_paused_foreground_task(scenario):
    from agent.runtime.recovery import new_run

    runtime, cfg, event, _, _, _ = scenario
    run = new_run(cfg, [], "paused-human-task")
    runtime.store.update_state(cfg, {"run": run})
    before = runtime.store.load(cfg)
    with pytest.raises(ValueError, match="event_deferred"):
        list(runtime.stream_events(event, cfg))
    after = runtime.store.load(cfg)
    assert after.run == before.run and after.messages == before.messages
