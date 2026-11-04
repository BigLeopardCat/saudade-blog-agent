"""Use model capacity without silently evicting history or spending on summaries."""

import json

import pytest
from _memory_support import ACTOR, Capture, config, run, runtime
from langchain_core.language_models.fake_chat_models import FakeListChatModel
from langchain_core.messages import HumanMessage, messages_to_dict
from langgraph.checkpoint.memory import InMemorySaver

from agent.api.schemas import ChatRequest
from agent.runtime import compaction, memory, retention
from agent.runtime.budget import BudgetLimits, TaskBudget
from agent.runtime.execution_state import initial_state
from agent.runtime.session import ToolSession
from config.settings import Settings, settings


def test_explicit_large_working_budget_keeps_history_without_premature_compaction(monkeypatch):
    monkeypatch.setattr(settings, "agent_context_tokens", 1000000)
    monkeypatch.setattr(settings, "agent_input_tokens", 1000000)
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
    prompt = selected.model_factory().prompts[-1]
    assert memory._tokens(prompt) <= settings.agent_input_tokens
    assert [m.id for m in saved[:len(source)]] == [m.id for m in source]
    assert "question-0:" in str(prompt)
    assert "question-319:" in str(prompt)
    assert not any(m.additional_kwargs.get("lc_source") for m in saved)


def test_full_request_schema_can_trigger_compaction_without_a_history_only_threshold(monkeypatch):
    monkeypatch.setattr(settings, "agent_summary_output_tokens", 512)
    monkeypatch.setattr("models.get_llm", lambda **_: FakeListChatModel(responses=["unused"]))
    calls = []
    monkeypatch.setattr(compaction, "summarize", lambda source, **_: calls.append(source) or "摘要")
    source = memory.turn_messages([HumanMessage(content="older " * 600)], [], "reply", ACTOR)
    source += memory.turn_messages([HumanMessage(content="recent")], [], "reply", ACTOR)
    session = ToolSession(Capture(), config(), {}, {})
    session.budget = TaskBudget(BudgetLimits(context_tokens=8000))
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


def test_oversized_history_is_compacted_in_bounded_steps_then_reused(monkeypatch):
    from math import ceil

    from agent.api.context import build_messages
    from agent.runtime.summarization import input_prompt

    monkeypatch.setattr(settings, "agent_context_tokens", 1000000)
    monkeypatch.setattr(settings, "agent_input_tokens", 4000)
    monkeypatch.setattr(settings, "agent_summary_output_tokens", 512)
    monkeypatch.setattr("models.get_llm", lambda **_: FakeListChatModel(responses=["unused"]))
    calls = []
    compacted = []
    monkeypatch.setattr(compaction, "record", lambda node, event, **data:
                        compacted.append(data) if event == "compacted" else None)
    monkeypatch.setattr(compaction, "summarize", lambda source, **kw: (
        calls.append(source) or "remembered-fact-0; older data is represented here."))
    model = Capture()
    selected, source = runtime(InMemorySaver(), model), []
    request = ChatRequest(message="保留当前要求：只读核对，不发送通知。")
    fixed = selected.application.prompt(build_messages(request), ACTOR)
    # The target must fit the actual product prefix plus reserved summary output.
    counter = TaskBudget(BudgetLimits(context_tokens=1000000))
    working = max(4000, ceil((counter.input_tokens(fixed) + 512)
                             / settings.agent_compaction_target))
    monkeypatch.setattr(settings, "agent_input_tokens", working)
    for number in range(16):
        source.extend(memory.turn_messages([HumanMessage(
            content=f"fact-{number}: " + "history " * 100)], [], "answer " * 100, ACTOR))
    selected.store.update_state(config(), {"messages": source})
    run(selected, request)
    assert len(calls) >= 2
    budget = TaskBudget(BudgetLimits(context_tokens=1000000, input_tokens=working))
    assert all(budget.call_block_reason(input_prompt(old), output_limit=512) is None
               for old in calls)
    assert memory._tokens(model.prompts[-1]) <= working
    assert "remembered-fact-0" in str(model.prompts[-1])
    assert "保留当前要求：只读核对，不发送通知。" in str(model.prompts[-1])
    count = len(calls)
    assert compacted[-1]["request_after_tokens"] <= compacted[-1]["target_tokens"]
    run(selected, ChatRequest(message="还记得先前的信息吗？"))
    assert len(calls) == count and "remembered-fact-0" in str(model.prompts[-1])


def test_cumulative_task_remainder_does_not_request_a_lossy_summary(monkeypatch):
    session = ToolSession(Capture(), config(), {}, {})
    session.budget = TaskBudget(BudgetLimits(context_tokens=1000000, task_tokens=1000))
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
