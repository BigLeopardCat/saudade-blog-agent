"""Bound current message state without losing source pairs or admitting deleted threads."""

import pytest
from langchain_core.language_models.fake_chat_models import FakeListChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph.message import add_messages

from agent import checkpoint
from agent.principal import SOURCE_ASSERTION, Principal
from agent.runtime import memory
from agent.runtime.checkpoint_store import ConversationDeleted
from agent.runtime.native import NativeRuntime
from agent.runtime.native_store import NativeConversationStore
from tools import base


def test_invalid_conninfo_never_exposes_credentials_in_the_error():
    with pytest.raises(RuntimeError) as error:
        checkpoint.backend_for("user=example password=PRIVATE_CREDENTIAL dbname=example")
    assert "PRIVATE_CREDENTIAL" not in str(error.value)


def test_checkpoint_selection_keeps_complete_source_pairs_and_the_current_turn():
    prior = []
    for number in range(20):
        call = {"name": "search_notes", "args": {"keyword": f"topic{number}"}, "id": str(number)}
        prior.extend([HumanMessage(content=f"question{number}"),
                      *memory.observation(call, base.ok("x" * 1300)), AIMessage(content="reply")])
    current = [HumanMessage(content="current"), AIMessage(content="answer")]
    selected = add_messages(prior, memory.checkpoint_messages(prior, current))
    assert selected[-2:] == current
    assert len(selected) == len(prior) + len(current)
    ids = {call["id"] for m in selected if isinstance(m, AIMessage) for call in m.tool_calls}
    results = {m.tool_call_id for m in selected if isinstance(m, ToolMessage)}
    assert ids and ids == results


def test_native_checkpoint_stays_bounded_after_many_turns_and_reload(monkeypatch):
    import models
    from config.settings import settings

    monkeypatch.setattr(settings, "agent_context_tokens", 10000)
    monkeypatch.setattr(settings, "agent_summary_output_tokens", 512)
    monkeypatch.setattr(models, "get_llm", lambda **kwargs: FakeListChatModel(
        responses=["summary"]))
    class Model:
        def bind_tools(self, tools, **kwargs):
            return self

        def invoke(self, prompt):
            return AIMessage(content="answer" * 150)

    saver = InMemorySaver()
    runtime = NativeRuntime(NativeConversationStore(saver), model_factory=lambda **_: Model())
    cfg = {"configurable": {"thread_id": "conv_bounded", "conversation_id": "bounded",
                            "principal": Principal(7, "user", SOURCE_ASSERTION), "user_id": 7}}
    for number in range(30):
        list(runtime.stream_events(
            {"messages": [HumanMessage(content=f"question{number}" + "x" * 600)]}, cfg))
        saved = runtime.get_state(cfg).values["messages"]
        assert memory._tokens(saved) <= settings.agent_context_tokens
    assert any(m.additional_kwargs.get("lc_source") == "summarization" for m in saved)
    assert saved[-2].content.startswith("question29")
    assert not any(m.content.startswith("question0") for m in saved)
    assert checkpoint.delete_thread(saver, "bounded") is True
    with pytest.raises(ConversationDeleted):
        list(runtime.stream_events({"messages": [HumanMessage(content="late")]}, cfg))
    assert runtime.get_state(cfg).values == {}
