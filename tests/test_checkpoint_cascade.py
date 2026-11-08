"""Checkpoint deletion requires verified results and authenticated outbox delivery."""

import asyncio

import pytest
from _memory_support import ACTOR
from _own_support import config
from _parent_repo import read
from _service import app, cleanup_headers
from langchain_core.messages import HumanMessage
from langgraph.checkpoint.memory import InMemorySaver

from agent import checkpoint
from agent.api.routes import conversations
from agent.runtime.checkpoint_conversations import CheckpointConversationStore


class ObservedSaver(InMemorySaver):
    def __init__(self, *, noop=False):
        super().__init__()
        self.calls = []
        self.noop = noop
        self.saver = self

    def delete_thread(self, thread_id):
        self.calls.append(thread_id)
        if not self.noop:
            super().delete_thread(thread_id)


def seed(saver):
    CheckpointConversationStore(saver).update_state(
        config(), {"messages": [HumanMessage(content="saved")]})


@pytest.mark.parametrize("present", [False, True])
def test_delete_verifies_absence_at_conversation_coordinate(present):
    saver = ObservedSaver()
    if present:
        seed(saver)
    assert checkpoint.delete_thread(saver, 42, principal=ACTOR)
    assert saver.calls == ["conv_42"]
    assert saver.get_tuple({"configurable": {"thread_id": "conv_42"}}) is None


def test_noop_delete_cannot_report_success():
    saver = ObservedSaver(noop=True)
    seed(saver)
    assert checkpoint.delete_thread(saver, 42, principal=ACTOR) is False


@pytest.mark.parametrize("value", [None, "", "   ", 0, "0", "None"])
def test_invalid_conversation_never_calls_saver(value):
    saver = ObservedSaver()
    assert checkpoint.delete_thread(saver, value, principal=ACTOR) is False
    assert saver.calls == []


@pytest.mark.parametrize("mode,conversation,expected", [
    ("missing", 42, {"deleted": False, "why": "no_saver"}),
    ("saved", 0, {"deleted": False, "why": "bad_conversation_id"}),
    ("saved", 42, {"deleted": True}),
    ("noop", 42, {"deleted": False, "why": "still_readable"}),
])
def test_endpoint_reports_real_deletion_result(
        monkeypatch, host_identity, mode, conversation, expected):
    saver = None if mode == "missing" else ObservedSaver(noop=mode == "noop")
    if saver:
        seed(saver)
    monkeypatch.setattr(app.state, "runtime", saver)
    request = type("Request", (), {"headers": cleanup_headers(conversation), "app": app})()
    result = asyncio.run(conversations.checkpoint_delete(conversation, request))
    assert all(result.get(key) == value for key, value in expected.items())


def function_body(source, name):
    start = source.find(name)
    assert start >= 0, f"missing host contract: {name}"
    opening = source.index("{", start)
    depth = 0
    for index in range(opening, len(source)):
        depth += (source[index] == "{") - (source[index] == "}")
        if not depth:
            return source[opening:index + 1]
    raise AssertionError(f"incomplete host function: {name}")


def test_host_deletion_paths_use_authenticated_durable_delivery():
    conversation = read("src/routes/conversation.rs", "会话删除须投递持久清理任务")
    account = read("src/routes/temp_user.rs", "账号删除须投递名下会话的清理任务")
    delivery = read("src/checkpoint_cleanup.rs", "两种删除共用鉴权交付与结果核实")
    if any(value is None for value in (conversation, account, delivery)):
        pytest.skip("public host contract unavailable")
    assert "checkpoint_cleanup::dispatch(&" in function_body(
        conversation, "async fn delete_conversation")
    assert "checkpoint_cleanup::dispatch(&" in function_body(account, "async fn delete_temp_user")
    purge = function_body(conversation, "async fn purge_conversation_data")
    assert "ConvScope::UserId(" in purge
    assert "checkpoint_cleanup::enqueue(db, row.id, row.user_id)" in conversation
    request = function_body(delivery, "async fn request")
    assert "X-Agent-Assertion" in request
    assert "client.delete(" in request and "/drop_turn" in request
    assert '"deleted"' in request and '"dropped"' in request
    assert 'return Err("deletion_unverified")' in request
    assert 'Some(true)' in request


pytestmark = pytest.mark.usefixtures("service_workers")
