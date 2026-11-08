"""Check real provider payload continuity through conversation commits and reloads."""

import uuid

import httpx
import pytest
from _framework_fixture import call, model_replies
from _own_support import config
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver

from agent.principal import SOURCE_ASSERTION, Principal
from agent.products.blog.application import BlogApplication
from agent.runtime import memory
from agent.runtime.checkpoint_conversations import CheckpointConversationStore
from agent.runtime.service import AgentRuntime
from models.deepseek import DeepSeekChat
from tools import base


def context(title):
    return HumanMessage(content="Page data", additional_kwargs={"memory_context": True,
                        "page_context": {"title": title}})


@pytest.mark.parametrize("next_role", ["admin", "user"])
def test_wire_prefix_keeps_context_parallel_calls_and_complete_sources_after_reload(
        monkeypatch, next_role):
    description = "BATCH_SOURCE_CANARY " + "metadata " * 180
    url = "https://images.example/selected.png"
    reads = []

    def handle(request):
        reads.append(request)
        ident = int(request.url.path.rsplit("/", 1)[-1])
        return httpx.Response(200, json={"code": 200, "data": {
            "imageKey": ident, "imageUrl": url, "displayName": f"image-{ident}",
            "description": description, "folderId": 0, "isFavorite": False,
            "version": "a" * 64}})

    with httpx.Client(transport=httpx.MockTransport(handle)) as backend, model_replies(
        {"tool_calls": [call("load_admin_gallery_info_tools", "load")]},
        {"reasoning_content": "PROVIDER_REPLAY_CANARY", "tool_calls": [
            call("get_gallery_image_metadata", "a", image_id=74),
            call("get_gallery_image_metadata", "b", image_id=75)]},
        {"content": "Read both metadata records."},
        {"content": "Follow-up."},
    ) as (model, requests):
        monkeypatch.setattr(base, "_client", backend)
        model = DeepSeekChat(model="deepseek-v4.1-flash", api_key="test-only",
            base_url="https://model.fixture.test/v1", http_client=model.http_client,
            streaming=True, stream_usage=True, max_tokens=128)
        saver = InMemorySaver()

        def runtime():
            return AgentRuntime(CheckpointConversationStore(saver), application=BlogApplication(),
                                model_factory=lambda **_: model)

        cfg = config(principal=Principal(7, "admin", SOURCE_ASSERTION))
        result = list(runtime().stream_events({"messages": [context("PAGE_A"),
            HumanMessage(content="Read image metadata 74 and 75.")]}, cfg))[-1]
        original = requests[-1]["messages"]
        saved = runtime().store.load(cfg).messages
        assert len(reads) == 2 and len(requests) == 3
        batch = next(m for m in saved if isinstance(m, AIMessage) and len(m.tool_calls) == 2)
        assert batch.additional_kwargs["reasoning_content"] == "PROVIDER_REPLAY_CANARY"
        assert "PROVIDER_REPLAY_CANARY" not in result.reply
        assert all(len(m.content) > 1400 and url in m.content for m in saved
                   if isinstance(m, ToolMessage) and m.name == "get_gallery_image_metadata")
        next_cfg = config(principal=Principal(7, next_role, SOURCE_ASSERTION))
        list(runtime().stream_events({"messages": [context("PAGE_B"),
             HumanMessage(content="Continue.")]}, next_cfg))
    current = requests[-1]["messages"]
    if next_role == "admin":
        assert current[:len(original)] == original
        assert requests[-1]["tools"] == requests[-2]["tools"]
        assert "PAGE_B" in current[-1]["content"]
        assert url in str(current) and "BATCH_SOURCE_CANARY" in str(current)
    else:
        assert all(value not in str(current) for value in
                   ("PAGE_A", url, "BATCH_SOURCE_CANARY", "PROVIDER_REPLAY_CANARY"))


def test_transcript_removes_approval_secrets_without_rewriting_visible_preparation():
    from agent.runtime import transcript

    approval_token = uuid.uuid4().hex
    user = HumanMessage(content="Prepare a change", id="user")
    call_id = {"name": "prepare_favorite_change", "args": {}, "id": "prepare"}
    prepared = ToolMessage(content='{"status":"awaiting_confirmation","executed":false}',
        name=call_id['name'], tool_call_id='prepare', artifact={
            "kind": "prepared", "phase": "awaiting_approval", "proposal": {
                "execution": {"pending_confirm": {"token": approval_token}}}})
    saved = transcript.finish([user, AIMessage(content="", tool_calls=[call_id]), prepared,
                               AIMessage(content="Please review.")], "Please review.",
                              Principal(7, "admin", SOURCE_ASSERTION), [user])
    assert approval_token not in str(saved)
    assert saved[2].content == prepared.content and saved[2].artifact == {"kind": "prepared"}


def test_budget_exit_keeps_a_closed_provider_exchange_without_claiming_execution():
    from agent.runtime import transcript

    user = HumanMessage(content="Read", id="user")
    saved = transcript.finish([user, AIMessage(content="", tool_calls=[{
        "id": "unrecorded", "name": "search_notes", "args": {"keyword": "topic"}}])],
        "Stopped.", Principal(7, "user", SOURCE_ASSERTION), [user])
    assert saved[-2].status == "error" and '"status": "unverified"' in saved[-2].content
    assert saved[-2].tool_call_id == "unrecorded" and saved[-1].content == "Stopped."
    assert memory.history(saved, Principal(7, "user", SOURCE_ASSERTION)) == saved
