"""Stable turn identity, durable cancellation fences and public checkpoint updates."""

import asyncio
from types import SimpleNamespace

import pytest
from _memory_support import Capture, config, runtime
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.memory import InMemorySaver
from pydantic import ValidationError

from agent import checkpoint
from agent.api.routes import conversations
from agent.runtime import checkpoint_store
from agent.runtime.control import AgentCancelled
from agent.runtime.memory import turn_message_id


def run(selected, text, task_id):
    return list(selected.stream_events({"task_id": task_id,
        "messages": [HumanMessage(content=text)]}, config()))[-1]


def test_repeated_discard_of_identical_text_never_deletes_the_previous_turn():
    model = Capture()
    selected = runtime(InMemorySaver(), model)
    run(selected, "继续", "first")
    first = list(selected.store.load(config()).messages)
    run(selected, "继续", "second")
    for _ in range(2):
        assert checkpoint.drop_last_user_turn(selected, 42, "second")["dropped"]
        assert selected.store.load(config()).messages == first
    calls = len(model.prompts)
    blocked = run(runtime(selected.saver, model), "继续", "second")
    assert blocked.execution["blocked"] == [{"reason": "turn_discarded"}]
    assert len(model.prompts) == calls
    assert run(selected, "继续", "third").reply


def test_discard_before_original_delivery_rejects_the_late_request():
    saver, model = InMemorySaver(), Capture()
    selected = runtime(saver, model)
    assert checkpoint.drop_last_user_turn(selected, 42, "late")["dropped"]
    rebuilt = runtime(saver, model)
    assert run(rebuilt, "迟到请求", "late").execution["blocked"]
    assert model.prompts == []
    assert rebuilt.store.load(config()).messages == []


def test_nonlast_target_is_refused_and_not_fenced():
    selected = runtime(InMemorySaver(), Capture())
    run(selected, "相同正文", "older")
    run(selected, "相同正文", "newer")
    before = selected.store.load(config()).messages
    assert checkpoint.drop_last_user_turn(selected, 42, "older") == {
        "dropped": False, "why": "not_last_turn"}
    assert selected.store.load(config()).messages == before
    assert not checkpoint_store.turn_deleted(selected.saver, "conv_42", "older")


def test_failed_checkpoint_update_keeps_the_fence_and_retry_finishes_cleanup(monkeypatch):
    selected = runtime(InMemorySaver(), Capture())
    run(selected, "清除这个轮次", "failed-update")
    real_update = selected.update_state
    monkeypatch.setattr(selected, "update_state", lambda *_: None)
    assert checkpoint.drop_last_user_turn(selected, 42, "failed-update") == {
        "dropped": False, "why": "still_present"}
    assert checkpoint_store.turn_deleted(selected.saver, "conv_42", "failed-update")
    monkeypatch.setattr(selected, "update_state", real_update)
    assert checkpoint.drop_last_user_turn(selected, 42, "failed-update")["dropped"]
    assert selected.store.load(config()).messages == []


def test_discard_interrupt_preserves_earlier_history_and_blocks_resume():
    selected = runtime(InMemorySaver(), Capture())
    run(selected, "旧请求", "kept")
    before = selected.store.load(config()).messages

    class Interrupted(Capture):
        def stream(self, *args, **kwargs):
            raise AgentCancelled()
            yield

    selected.model_factory = lambda **_: Interrupted()
    with pytest.raises(AgentCancelled):
        run(selected, "被打断的请求", "interrupted")
    assert selected.store.load(config()).run
    assert checkpoint.drop_last_user_turn(selected, 42, "interrupted")["dropped"]
    assert selected.store.load(config()).messages == before
    assert not selected.store.load(config()).run
    result = list(selected.stream_events({"resume_task": "interrupted"}, config()))[-1]
    assert result.execution["blocked"] == [{"reason": "turn_discarded"}]


def test_same_task_steering_is_removed_with_its_original_request():
    from agent.runtime.task_inputs import messages

    selected = runtime(InMemorySaver(), Capture())
    run(selected, "处理文章", "steered")
    extras = messages([{"id": "constraint", "text": "只处理草稿"}], "steered")
    selected.update_state(config(), {"messages": [*extras, AIMessage(content="已核对草稿。") ]})
    assert checkpoint.drop_last_user_turn(selected, 42, "steered")["dropped"]
    assert selected.store.load(config()).messages == []


def test_multimodal_discard_does_not_compare_or_parse_the_message_text():
    selected = runtime(InMemorySaver(), Capture())
    result = list(selected.stream_events({"task_id": "image", "messages": [HumanMessage(content=[
        {"type": "text", "text": "看图\\n[图片]"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AA"}}])]}, config()))
    assert result[-1].reply
    assert checkpoint.drop_last_user_turn(selected, 42, "image")["dropped"]
    assert selected.store.load(config()).messages == []


def test_drop_endpoint_requires_stable_target_and_uses_authenticated_worker(monkeypatch):
    with pytest.raises(ValidationError):
        conversations.DropTurnReq(text="继续")
    selected = runtime(InMemorySaver(), Capture())
    run(selected, "继续", "endpoint")

    class Worker:
        closing = False

        async def run(self, fn, *args):
            return await asyncio.to_thread(fn, *args)

    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(
        runtime=selected, workers=Worker())))
    monkeypatch.setattr(conversations, "_resolve_principal", lambda *_: SimpleNamespace(uid=7))
    result = asyncio.run(conversations.checkpoint_drop_turn("42",
        conversations.DropTurnReq(task_id="endpoint", uid=7), request))
    assert result["dropped"] and selected.store.load(config()).messages == []
    assert turn_message_id("endpoint") == "user-turn:endpoint"
