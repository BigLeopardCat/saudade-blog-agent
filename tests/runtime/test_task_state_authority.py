"""Committed framework state wins over local views and older message projections."""

import pytest
from _memory_support import Capture, config
from langchain.tools import ToolRuntime
from langchain_core.messages import AIMessageChunk, HumanMessage, ToolMessage
from langchain_core.tools import tool
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from agent.products.blog.capabilities import ToolCatalog
from agent.runtime.execution_state import capture, initial_state, restore
from agent.runtime.native import NativeRuntime
from agent.runtime.native_store import NativeConversationStore
from agent.runtime.results import TurnResult
from agent.runtime.session import ToolSession
from agent.runtime.turns import AgentCancelled


def call(name, ident):
    return {"name": name, "args": {}, "id": ident}


def test_same_batch_commands_merge_once_and_survive_the_next_model_boundary():
    @tool
    def first(runtime: ToolRuntime) -> Command:
        """Store the first synthetic task effect."""
        return Command(update={"effects": {"first": "committed", "value": "old"}, "messages": [
            ToolMessage(content="first", tool_call_id=runtime.tool_call_id)]})

    @tool
    def second(runtime: ToolRuntime) -> Command:
        """Store the second synthetic task effect."""
        return Command(update={"effects": {"second": "committed", "value": "new"}, "messages": [
            ToolMessage(content="second", tool_call_id=runtime.tool_call_id)]})

    @tool
    def plain() -> str:
        """Read a local synthetic value."""
        return "read"

    seen = []

    @tool
    def inspect_state(runtime: ToolRuntime) -> str:
        """Read the committed effects at the next framework step."""
        seen.append(dict(runtime.state["effects"]))
        return "checked"

    model = Capture(AIMessageChunk(content="", tool_calls=[
        call("first", "a"), call("second", "b"), call("plain", "c")]),
        AIMessageChunk(content="", tool_calls=[call("inspect_state", "inspect")]),
        AIMessageChunk(content="done"))
    tools = {t.name: t for t in (first, second, plain, inspect_state)}
    result = ToolSession(model, config(), tools, {}).run([HumanMessage(content="run fixture")])
    expected = {"first": "committed", "second": "committed", "value": "new"}
    assert seen == [expected]
    assert result.execution == {**expected, "receipts": []}
    messages = [m for m in model.prompts[-1] if isinstance(m, ToolMessage)]
    assert [m.tool_call_id for m in messages] == ["a", "b", "c", "inspect"]


def test_a_new_task_resets_effects_even_when_reusing_the_same_persistent_child():
    @tool
    def task_effect(runtime: ToolRuntime) -> Command:
        """Record a synthetic task effect."""
        return Command(update={"effects": {"old_task": True}, "messages": [
            ToolMessage(content="stored", tool_call_id=runtime.tool_call_id)]})

    model = Capture(AIMessageChunk(content="", tool_calls=[call("task_effect", "effect")]),
                    AIMessageChunk(content="first done"), AIMessageChunk(content="new task"))
    selected = NativeRuntime(NativeConversationStore(InMemorySaver()),
        catalog=ToolCatalog(read_tools={task_effect.name: task_effect}),
        model_factory=lambda **_: model)
    first = list(selected.stream_events(
        {"messages": [HumanMessage(content="first")]}, config()))[-1]
    assert first.execution == {"old_task": True, "receipts": []}
    second = list(selected.stream_events(
        {"messages": [HumanMessage(content="second")]}, config()))[-1]
    assert second.reply == "new task" and second.execution is None


def test_resume_does_not_replace_committed_effects_with_an_older_message_projection():
    calls = []

    @tool
    def task_effect(runtime: ToolRuntime) -> Command:
        """Record the current synthetic effect with an older display projection."""
        calls.append(1)
        return Command(update={"effects": {"value": "current"}, "messages": [ToolMessage(
            content="stored", tool_call_id=runtime.tool_call_id,
            artifact={"kind": "ok", "effects": {"value": "old"}})]})

    class Interrupted(Capture):
        def stream(self, messages, **kwargs):
            self.prompts.append(list(messages))
            if len(self.prompts) == 1:
                yield AIMessageChunk(content="", tool_calls=[call("task_effect", "effect")])
            elif len(self.prompts) == 2:
                raise AgentCancelled()
            else:
                yield AIMessageChunk(content="continued")

    model, saver = Interrupted(), InMemorySaver()

    def selected():
        return NativeRuntime(NativeConversationStore(saver),
            catalog=ToolCatalog(read_tools={task_effect.name: task_effect}),
            model_factory=lambda **_: model)

    with pytest.raises(AgentCancelled):
        list(selected().stream_events(
            {"messages": [HumanMessage(content="record fixture")]}, config()))
    task = selected().store.load(config()).run
    result = list(selected().stream_events({"resume_task": task["id"]}, config()))[-1]
    assert calls == [1] and result.reply == "continued"
    assert result.execution == {"value": "current", "receipts": []}


def test_checkpoint_updates_are_frozen_and_explicit_empty_state_clears_local_values():
    session = ToolSession(object(), {}, {}, {})
    session.state["effects"] = {"value": [1]}
    session.state["applied_inputs"] = ["stale"]
    session.outcome = TurnResult("old terminal")
    saved = capture(session)
    session.state["effects"]["value"].append(2)
    assert saved["effects"] == {"value": [1]}
    restore(session, initial_state())
    assert session.outcome is None
    assert session.state["applied_inputs"] == [] and session.state["effects"] == {}
