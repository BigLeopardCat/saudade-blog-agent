"""Native storage adopts conversation data without loading the retired planner."""

import asyncio
import secrets
from typing import Annotated, TypedDict

import pytest
from _own_support import config
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages

from agent import checkpoint
from agent.runtime import checkpoint_store
from agent.runtime.native import NativeRuntime
from agent.runtime.native_store import NativeConversationStore


def legacy_checkpoint(saver, *, pending=None):
    class LegacyState(TypedDict, total=False):
        messages: Annotated[list, add_messages]
        pending_confirm: dict
        pending_action: dict
        plan: list
        done: bool

    def must_not_run(state):
        pytest.fail("legacy planner ran")

    graph = StateGraph(LegacyState)
    graph.add_node("planner", must_not_run)
    graph.add_edge(START, "planner")
    graph.add_edge("planner", END)
    old = graph.compile(checkpointer=saver, interrupt_before=["planner"])
    old.invoke({"messages": [HumanMessage(content="旧问题"), AIMessage(content="旧答复")],
                "plan": [{"tool": "forbidden_write"}], "done": False, **(pending or {})}, config())
    assert old.get_state(config()).next == ("planner",)
    return old


def test_reads_old_checkpoint_without_exposing_plan_or_continuing_old_nodes():
    saver = InMemorySaver()
    legacy_checkpoint(saver)
    store = NativeConversationStore(saver)
    previous = store.load(config())
    assert [m.content for m in previous.messages] == ["旧问题", "旧答复"]
    assert not previous.unfinished and not store.is_resuming(config())
    assert "plan" not in store.get_state(config()).values
    store.save_turn(config(), [HumanMessage(content="新问题")], "新答复")
    rebuilt = NativeConversationStore(saver)
    assert [m.content for m in rebuilt.load(config()).messages] == [
        "旧问题", "旧答复", "新问题", "新答复"]
    assert rebuilt.get_state(config()).next == ()


def test_old_pending_is_preserved_as_data_and_execution_cannot_write_plan_fields():
    saver = InMemorySaver()
    pending = {"pending_confirm": {"token": secrets.token_urlsafe(16)},
               "pending_action": {"jti": "old"}}
    legacy_checkpoint(saver, pending=pending)
    store = NativeConversationStore(saver)
    assert store.load(config()).pending == pending
    store.save_turn(config(), [HumanMessage(content="闲聊")], "你好", pending=pending,
                    execution={"plan": [{"tool": "must_not_be_used"}], "receipts": []})
    assert store.load(config()).pending == pending
    assert "plan" not in store.get_state(config()).values


def test_clear_pending_requires_exact_card_and_preserves_history_and_receipts():
    store = NativeConversationStore(InMemorySaver())
    pending = {"pending_confirm": {"q": "待确认"}, "pending_action": {"jti": "new"}}
    store.save_turn(config(), [HumanMessage(content="请求")], "答复",
                    execution={**pending, "receipts": [{"tool": "earlier_write"}]})
    store.clear_pending(config(), "old")
    assert store.load(config()).has_pending
    before = store.load(config()).messages
    store.clear_pending(config(), "new")
    assert not store.load(config()).has_pending
    assert store.load(config()).messages == before
    assert store.get_state(config()).values["receipts"] == [{"tool": "earlier_write"}]


def test_discard_after_legacy_adoption_uses_commit_node_and_delete_rejects_late_write():
    saver = InMemorySaver()
    legacy_checkpoint(saver)
    store = NativeConversationStore(saver)
    runtime = NativeRuntime(store)
    result = asyncio.run(checkpoint.drop_last_user_turn(runtime, 42, "旧问题"))
    assert result == {"dropped": True, "why": ""}
    assert store.load(config()).messages == []
    store.save_turn(config(), [HumanMessage(content="另一轮")], "答复")
    assert checkpoint.delete_thread(saver, 42)
    with pytest.raises(checkpoint_store.ConversationDeleted):
        store.save_turn(config(), [], "迟到答复")
    assert saver.get_tuple(config()) is None


def test_no_saver_remains_explicitly_stateless():
    store = NativeConversationStore(None)
    assert not store.available and store.load(config()).messages == []
    store.save_turn(config(), [HumanMessage(content="问题")], "答复")
    assert store.load(config()).messages == []
