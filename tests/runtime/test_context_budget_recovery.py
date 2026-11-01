"""Bounded compression checkpoints survive interruption and approval restoration."""

import pytest
import test_confirmation_continuation as continuation
from _memory_support import ACTOR, Capture, config, run, runtime
from langchain_core.language_models.fake_chat_models import FakeListChatModel
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.memory import InMemorySaver

from agent import confirm
from agent.api.schemas import ChatRequest
from agent.runtime import compaction, memory
from agent.runtime.turns import AgentCancelled
from config.settings import settings

backend = continuation.backend


def history(size=16):
    source = []
    for number in range(size):
        source.extend(memory.turn_messages([HumanMessage(
            content=f"fact-{number}: " + "history " * 100)], [], "answer " * 100, ACTOR))
    return source


def test_interruption_reuses_completed_compaction_without_restoring_covered_source(monkeypatch):
    monkeypatch.setattr(settings, "agent_context_tokens", 1000000)
    monkeypatch.setattr(settings, "agent_input_tokens", 4000)
    monkeypatch.setattr(settings, "agent_summary_output_tokens", 512)
    monkeypatch.setattr("models.get_llm", lambda **_: FakeListChatModel(responses=["unused"]))
    calls = []

    def summarize(source, **kwargs):
        calls.append([message.id for message in source])
        if len(calls) == 2:
            raise AgentCancelled()
        return "remembered-fact-0; already covered source remains represented."

    monkeypatch.setattr(compaction, "summarize", summarize)
    saver, model = InMemorySaver(), Capture()
    selected = runtime(saver, model)
    source = history()
    selected.store.update_state(config(), {"messages": source})
    with pytest.raises(AgentCancelled):
        run(selected, ChatRequest(message="只读，不通知。"))
    task = selected.store.load(config()).run["id"]
    assert not model.prompts and calls[0]
    selected = runtime(saver, model)
    list(selected.stream_events({"resume_task": task}, config()))
    assert calls[0][0] not in {ident for batch in calls[2:] for ident in batch}
    assert "remembered-fact-0" in str(model.prompts[-1])
    assert "只读，不通知。" in str(model.prompts[-1])
    assert not selected.store.load(config()).unfinished


def test_approval_restoration_preserves_task_with_a_smaller_input_budget(backend, monkeypatch):
    state, _ = backend
    monkeypatch.setattr(settings, "agent_context_tokens", 1000000)
    monkeypatch.setattr(settings, "agent_input_tokens", 128000)
    monkeypatch.setattr(settings, "agent_summary_output_tokens", 512)
    model = continuation.Model(AIMessage(content="", tool_calls=[
        continuation.call("load_favorites_info_tools", "load")]),
        AIMessage(content="", tool_calls=[continuation.FAVORITE]),
        AIMessage(content="请确认收藏。"))
    selected = continuation.runtime(model)
    selected.store.update_state(config(), {"messages": history(40)})
    card = continuation.run(selected, "只收藏文章19。")
    grant = confirm.verify(card.execution["pending_confirm"]["token"], 7, 42)
    monkeypatch.setattr(settings, "agent_input_tokens", 12000)
    summaries = []
    monkeypatch.setattr("models.get_llm", lambda **_: FakeListChatModel(responses=["unused"]))
    monkeypatch.setattr(compaction, "summarize", lambda source, **kw: (
        summaries.append(source) or "较早的对话事实已汇总。"))
    final = continuation.Model(AIMessage(content="收藏好了。"))
    selected.model_factory = lambda **_: final
    done = continuation.run(selected, grant=grant)
    assert summaries and done.reply == "收藏好了。"
    assert state["favorites"] == {19} and len(state["writes"]) == 1
    assert len(final.prompts) == 1 and "只收藏文章19。" in str(final.prompts[0])
    assert not selected.store.load(config()).has_pending
