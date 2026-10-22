"""Overlapping turns must not overwrite history or race discard/delete."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from threading import Event

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.memory import InMemorySaver

from agent import checkpoint
from agent.agent import create_agent
from agent.graph import AgentCancelled, graph_input
from agent.runtime import turns
from agent.runtime.hybrid import HybridArm


def _config(thread_id="conv_overlap", stop_event=None):
    return {"configurable": {"thread_id": thread_id, "stop_event": stop_event}}


@pytest.mark.parametrize("operation", ["turn", "discard", "delete"])
def test_turn_serializes_history_and_lifecycle(monkeypatch, operation):
    saver = InMemorySaver()
    arm = HybridArm(create_agent(checkpointer=saver), {}, {}, {})
    entered, release, attempted = Event(), Event(), Event()
    prompts = []

    class Model:
        def bind_tools(self, tools, **kwargs):
            return self

        def invoke(self, prompt):
            prompts.append([m.content for m in prompt])
            if len(prompts) == 1:
                entered.set()
                assert release.wait(5), "test did not release first turn"
                return AIMessage(content="第一轮回复")
            assert "第一轮回复" in prompts[1]
            return AIMessage(content="第二轮回复")

    monkeypatch.setattr("agent.runtime.hybrid.get_llm", lambda **_: Model())

    def turn(text):
        return list(arm.stream(graph_input([HumanMessage(content=text)]), _config()))

    def overlap():
        attempted.set()
        if operation == "turn":
            return turn("第二轮")
        if operation == "discard":
            return asyncio.run(checkpoint.drop_last_user_turn(arm, "overlap", "第一轮"))
        return checkpoint.delete_thread(saver, "overlap")

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(turn, "第一轮")
        try:
            assert entered.wait(5)
            second = pool.submit(overlap)
            assert attempted.wait(5)
            with pytest.raises(FutureTimeout):
                second.result(timeout=0.1)
        finally:
            release.set()
        first.result(timeout=5)
        result = second.result(timeout=5)

    snapshot = arm.get_state(_config())
    if operation == "turn":
        assert [m.content for m in snapshot.values["messages"]] == [
            "第一轮", "第一轮回复", "第二轮", "第二轮回复"]
    elif operation == "discard":
        assert result == {"dropped": True, "why": ""}
        assert snapshot.values["messages"] == []
    else:
        assert result is True
        assert snapshot.values == {}


def test_cancelled_waiter_never_acquires_turn_lock():
    saver = InMemorySaver()
    stop, attempted = Event(), Event()

    def wait():
        attempted.set()
        with turns.conversation_turn(saver, _config(stop_event=stop)):
            raise AssertionError("cancelled waiter entered the turn")

    with ThreadPoolExecutor(max_workers=1) as pool:
        with turns.conversation_turn(saver, _config()):
            future = pool.submit(wait)
            assert attempted.wait(5)
            stop.set()
            with pytest.raises(AgentCancelled):
                future.result(timeout=5)
    assert not turns._local_locks


def test_timeout_and_exception_release_local_lock_entries():
    saver = InMemorySaver()
    with turns.conversation_turn(saver, _config()):
        with pytest.raises(TimeoutError):
            with turns.conversation_turn(saver, _config(), timeout=0.01):
                raise AssertionError("overlapping turn entered")
        with turns.conversation_turn(saver, _config("conv_other")):
            pass
    with pytest.raises(ValueError):
        with turns.conversation_turn(saver, _config()):
            raise ValueError("failed turn")
    assert not turns._local_locks
