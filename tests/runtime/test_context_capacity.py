"""Use model capacity without silently evicting history or spending on summaries."""

import json

import pytest
from _memory_support import ACTOR, Capture, config, run, runtime
from langchain_core.language_models.fake_chat_models import FakeListChatModel
from langchain_core.messages import HumanMessage, messages_to_dict
from langgraph.checkpoint.memory import InMemorySaver

from agent.api.schemas import ChatRequest
from agent.runtime import compaction, memory, retention
from agent.runtime.budget import BudgetLimits, RequestBudget
from agent.runtime.execution_state import initial_state
from agent.runtime.session import ToolSession
from config.settings import Settings, settings


def test_large_window_keeps_more_than_256_turns_and_one_mib_without_compacting(monkeypatch):
    monkeypatch.setattr(settings, "agent_context_tokens", 1000000)
    monkeypatch.setattr(compaction, "summarize", lambda *a, **k: pytest.fail("premature summary"))
    selected, source = runtime(InMemorySaver(), Capture()), []
    for number in range(320):
        source.extend(memory.turn_messages(
            [HumanMessage(content=f"question-{number}: " + "history " * 200)], [],
            "answer " * 200, ACTOR))
    assert memory._tokens(source) > 100000
    assert len(json.dumps(messages_to_dict(source)).encode()) > 1024 * 1024
    assert retention.retain(source) == source
    selected.store.update_state(config(), {"messages": source})
    run(selected, ChatRequest(message="继续"))
    saved = selected.store.load(config()).messages
    assert [m.id for m in saved[:len(source)]] == [m.id for m in source]
    assert "question-0:" in str(selected.model_factory().prompts[-1])
    assert not any(m.additional_kwargs.get("lc_source") for m in saved)


def test_full_request_schema_can_trigger_compaction_without_a_history_only_threshold(monkeypatch):
    monkeypatch.setattr(settings, "agent_summary_output_tokens", 512)
    monkeypatch.setattr("models.get_llm", lambda **_: FakeListChatModel(responses=["unused"]))
    calls = []
    monkeypatch.setattr(compaction, "summarize", lambda source, **_: calls.append(source) or "摘要")
    source = memory.turn_messages([HumanMessage(content="older " * 600)], [], "reply", ACTOR)
    source += memory.turn_messages([HumanMessage(content="recent")], [], "reply", ACTOR)
    session = ToolSession(Capture(), config(), {}, {})
    session.budget = RequestBudget(BudgetLimits(context_tokens=8000))
    session.menu = [{"type": "function", "function": {
        "name": "fixture", "description": "large schema " * 1800,
        "parameters": {"type": "object", "properties": {}}}}]
    # Use the same authorized schema at the policy boundary.
    monkeypatch.setattr(compaction, "restore", lambda *args: None)
    state = {**initial_state(), "messages": [*source, HumanMessage(content="current", id="root")]}
    for i, message in enumerate(source):
        message.id = str(i)
    update = compaction.ConversationCompaction(session).before_model(state, None)
    assert calls and update["context_update"]["covered_ids"]
    assert update["context_anchor"] == "root"


def test_cumulative_task_remainder_does_not_request_a_lossy_summary(monkeypatch):
    session = ToolSession(Capture(), config(), {}, {})
    session.budget = RequestBudget(BudgetLimits(context_tokens=1000000, request_tokens=1000))
    monkeypatch.setattr(compaction, "summarize",
                        lambda *a, **k: pytest.fail("cost is not capacity"))
    state = {**initial_state(), "messages": [HumanMessage(content="old " * 3000, id="root")]}
    update = compaction.ConversationCompaction(session).before_model(state, None)
    assert not update or "messages" not in update
    assert session.budget.model_calls == 0


@pytest.mark.parametrize("changes", [
    {"agent_compaction_trigger": 1}, {"agent_compaction_target": 0},
    {"agent_compaction_trigger": 0.5, "agent_compaction_target": 0.7},
])
def test_invalid_compaction_watermarks_are_rejected(changes):
    with pytest.raises(ValueError):
        Settings(_env_file=None, **changes)
