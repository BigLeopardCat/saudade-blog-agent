"""Durable image results reach the framework graph and the user's visible reply."""

import hashlib
import json
import secrets

import httpx
import pytest
from _own_support import config
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult
from langgraph.checkpoint.memory import InMemorySaver
from pydantic import Field

from agent import confirm
from agent.principal import SOURCE_ASSERTION, Principal
from agent.products.blog.application import BlogApplication
from agent.runtime.checkpoint_conversations import CheckpointConversationStore
from agent.runtime.service import AgentRuntime
from tools import base

ID = "6b9e2bf1-56af-428c-8177-e3ad0641e036"
VERSION = "c" * 64
EVENT_ID = "image_" + hashlib.sha256(f"{ID}:{VERSION}".encode()).hexdigest()


class Model(BaseChatModel):
    max_tokens: int = 128
    reply: str = "图片已经做好啦，喜欢的话就保留喵。"
    calls: list = Field(default_factory=list)
    options: list = Field(default_factory=list)

    @property
    def _llm_type(self):
        return "image-event-fixture"

    def bind_tools(self, tools, **kwargs):
        self.options.append(kwargs)
        return self.bind(**kwargs)

    def _generate(self, prompt, stop=None, run_manager=None, **kwargs):
        self.calls.append(prompt)
        return ChatResult(generations=[ChatGeneration(message=AIMessage(
            content=self.reply))])

    def _stream(self, prompt, stop=None, run_manager=None, **kwargs):
        reply = self._generate(prompt, **kwargs).generations[0].message
        yield ChatGenerationChunk(message=AIMessageChunk(content=reply.content))


@pytest.fixture
def scenario(monkeypatch):
    job = {"id": ID, "requestHash": "a" * 64, "status": "review", "imageRemoved": False,
           "conversationId": 42, "eventVersion": VERSION, "outputHash": "b" * 64,
           "previewUrl": f"/api/public/image-generation/{ID}/preview?token=PRIVATE",
           "request": {"provider": "https://images.test/v1", "model": "fixture",
                       "prompt": "approved prompt", "size": "1536x1024",
                       "quality": "high", "references": []}}
    requests = []

    def host(request):
        assert request.method == "GET", "completion must never start or save an image"
        assert request.url.path.endswith(ID)
        requests.append(request)
        return httpx.Response(200, json={"code": 200, "data": job})

    monkeypatch.setattr(confirm.settings, "confirmation_secret", secrets.token_urlsafe(32))
    with httpx.Client(transport=httpx.MockTransport(host)) as client:
        monkeypatch.setattr(base, "_client", client)
        saver, model = InMemorySaver(), Model()
        cfg = config(principal=Principal(7, "admin", SOURCE_ASSERTION))
        cfg["configurable"]["turn_id"] = EVENT_ID
        runtime = AgentRuntime(CheckpointConversationStore(saver), application=BlogApplication(),
                               model_factory=lambda **_: model)
        runtime.store.update_state(cfg, {"messages": [HumanMessage(
            content="请生成图片，做好后让我决定是否保留。", id="user-turn:original",
            additional_kwargs={"memory_actor": {"uid": 7, "role": "admin"}})]})
        event = {"task_id": EVENT_ID, "turn_id": EVENT_ID, "system_event": {
            "kind": "image_generation", "job_id": ID, "version": VERSION}}
        yield runtime, cfg, event, job, model, requests


def test_completion_seeds_one_native_tool_and_one_model_call_then_reopens_without_replay(scenario):
    runtime, cfg, event, _, model, requests = scenario
    result = list(runtime.stream_events(event, cfg))[-1]
    card = result.execution["pending_confirm"]
    assert card["image_review"]["result_url"].endswith("token=PRIVATE")
    assert len(model.calls) == 1 and model.options[-1]["tool_choice"] == "none"
    wire = json.dumps([{"content": message.content, "kwargs": message.additional_kwargs,
                        "calls": getattr(message, "tool_calls", [])} for message in model.calls[0]])
    assert "PRIVATE" not in wire
    assert "awaiting_confirmation" in wire
    assert [message.content for message in runtime.store.load(cfg).messages
            if isinstance(message, HumanMessage)] == ["请生成图片，做好后让我决定是否保留。"]
    replay = list(runtime.stream_events(event, cfg))[-1]
    assert replay.execution["pending_confirm"] == card
    assert len(model.calls) == 1 and len(requests) == 2


@pytest.mark.parametrize("change", ["owner", "version", "status", "role"])
def test_completion_cannot_trust_forged_or_stale_browser_status(scenario, change):
    runtime, cfg, event, job, model, _ = scenario
    if change == "owner":
        job["conversationId"] = 43
    elif change == "version":
        job["eventVersion"] = "d" * 64
    elif change == "status":
        job["status"] = "generating"
    else:
        cfg["configurable"]["principal"] = Principal(7, "user", SOURCE_ASSERTION)
    with pytest.raises(ValueError, match="image_event_"):
        list(runtime.stream_events(event, cfg))
    assert not model.calls


def test_another_completion_waits_without_revoking_the_current_human_approval(scenario):
    runtime, cfg, event, job, model, _ = scenario
    original = list(runtime.stream_events(event, cfg))[-1]
    job["eventVersion"] = "d" * 64
    newer_id = "image_" + hashlib.sha256(f"{ID}:{job['eventVersion']}".encode()).hexdigest()
    cfg["configurable"]["turn_id"] = newer_id
    newer = {"task_id": newer_id, "system_event": {
        **event["system_event"], "version": job["eventVersion"]}}
    with pytest.raises(ValueError, match="image_event_deferred"):
        list(runtime.stream_events(newer, cfg))
    assert runtime.store.load(cfg).pending_confirm == original.execution["pending_confirm"]
    assert len(model.calls) == 1


def test_completion_survives_empty_human_history_without_fabricating_a_user_turn(scenario):
    _, cfg, event, _, model, _ = scenario
    runtime = AgentRuntime(CheckpointConversationStore(InMemorySaver()),
        application=BlogApplication(), model_factory=lambda **_: model)
    first = list(runtime.stream_events(event, cfg))[-1]
    saved = runtime.store.load(cfg)
    assert not any(isinstance(message, HumanMessage) for message in saved.messages)
    assert saved.messages[0].additional_kwargs["memory_actor"] == {"uid": 7, "role": "admin"}
    second = list(runtime.stream_events(event, cfg))[-1]
    assert first.execution["pending_confirm"] == second.execution["pending_confirm"]
    assert len(model.calls) == 1


@pytest.mark.parametrize("reply", ["已保留喵。", "![图片](/api/public/media/PLACEHOLDER)",
    "![图片](/api/public/media/22222222-2222-4222-8222-222222222222)",
    "![图片](/api/public/media/11111111-1111-4111-8111-111111111111)"])
def test_accepted_completion_delivers_real_image_once_in_stream_and_checkpoint(scenario, reply):
    runtime, cfg, event, job, model, requests = scenario
    url = "/api/public/media/11111111-1111-4111-8111-111111111111"
    job.update(status="accepted", imageId=77, previewUrl=url)
    model.reply = reply
    events = list(runtime.stream_events(event, cfg))
    result = events[-1]
    streamed = "".join(str(item.content) for item in events if isinstance(item, AIMessageChunk))
    assert result.reply.count(f"]({url})") == 1
    assert "PLACEHOLDER" not in result.reply + streamed
    assert streamed == result.reply
    assert runtime.store.load(cfg).messages[-1].content == result.reply
    assert not (result.execution or {}).get("pending_confirm")
    assert len(model.calls) == 1 and len(requests) == 2
    assert url in str(model.calls[0])
    replay = list(runtime.stream_events(event, cfg))[-1]
    assert replay.reply == result.reply
    assert len(model.calls) == 1 and len(requests) == 2


def test_saved_image_is_reusable_but_does_not_attach_to_unrelated_next_turn(scenario):
    from langchain.tools import ToolRuntime

    from agent.domains.gallery.reads import show_gallery_image

    runtime, cfg, event, job, model, requests = scenario
    url = "https://images.test/accepted.png"
    job.update(status="accepted", imageId=77, previewUrl=url)
    list(runtime.stream_events(event, cfg))
    loaded = runtime.store.load(cfg)
    tool_runtime = ToolRuntime(state={"messages": loaded.messages}, context=None, config=cfg,
        stream_writer=lambda _: None, tool_call_id="show", store=None)
    shown = show_gallery_image.func(77, tool_runtime)
    assert shown.meta["gallery_display"]["url"] == url
    assert len(requests) == 2
    model.reply = "不客气喵。"
    cfg["configurable"]["turn_id"] = "next-chat"
    result = list(runtime.stream_events({"message": "谢谢", "task_id": "next-chat"}, cfg))[-1]
    assert result.reply == "不客气喵。" and len(requests) == 2


@pytest.mark.parametrize("url", [None,
    f"/api/public/image-generation/{ID}/preview?token=PRIVATE",
    "https://images.test/preview?token=PRIVATE", "javascript:bad()"])
def test_accepted_job_without_public_link_never_exposes_preview_credentials(scenario, url):
    runtime, cfg, event, job, model, _ = scenario
    job.update(status="accepted", imageId=77, previewUrl=url)
    result = list(runtime.stream_events(event, cfg))[-1]
    assert "![" not in result.reply
    assert "PRIVATE" not in str(model.calls)


def test_removed_saved_image_is_not_attached(scenario):
    runtime, cfg, event, job, model, _ = scenario
    job.update(status="accepted", imageId=77, imageRemoved=True, previewUrl=None)
    result = list(runtime.stream_events(event, cfg))[-1]
    assert "![" not in result.reply
    assert "imageRemoved" in str(model.calls)
