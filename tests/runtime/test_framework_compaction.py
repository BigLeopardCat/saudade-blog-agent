"""Standard summarization must survive the application's conversation boundary."""

from types import SimpleNamespace

import pytest
from _memory_support import ACTOR, Capture, config, run, runtime
from langchain_core.language_models.fake_chat_models import FakeListChatModel
from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph

from agent.api.schemas import ChatRequest
from agent.principal import SOURCE_ASSERTION, Principal
from agent.runtime import compaction, memory
from agent.runtime.approval import ConversationState
from agent.runtime.budget import BudgetLimits, RequestBudget
from agent.runtime.summarization import SummaryUnavailable, summarize
from agent.runtime.turns import AgentCancelled


def seed(selected, count=16):
    messages = []
    for number in range(count):
        messages.extend(memory.turn_messages(
            [HumanMessage(content=f"question-{number}: " + "earlier request " * 18)], [],
            "earlier reply " * 18, ACTOR))
    selected.store.update_state(config(), {"messages": messages})
    return selected.store.load(config()).messages


@pytest.fixture
def summary_model(monkeypatch):
    import models

    model = FakeListChatModel(responses=["SUMMARY_CANARY: 用户希望继续核对维护事项。"])
    monkeypatch.setattr(models, "get_llm", lambda **kwargs: model)
    return model


def test_compaction_commits_summary_and_replacement_together_then_reloads(summary_model):
    saver, model = InMemorySaver(), Capture()
    selected = runtime(saver, model)
    before = seed(selected)
    run(selected, ChatRequest(message="继续核对"))
    saved = selected.store.load(config()).messages
    summaries = [m for m in saved if m.additional_kwargs.get("lc_source") == "summarization"]
    assert len(summaries) == 1 and "SUMMARY_CANARY" in summaries[0].content
    assert summaries[0].additional_kwargs["memory_actor"] == {"uid": 7, "role": "user"}
    assert len(saved) < len(before)
    assert saved[-2].content == "[当前问题]: 继续核对"
    assert "SUMMARY_CANARY" not in "".join(event.content for event in
        run(runtime(saver, model), ChatRequest(message="第二条呢"))
        if isinstance(event, AIMessageChunk))
    assert "SUMMARY_CANARY" in str(model.prompts[-1])
    assert not any("question-0:" in str(m.content) for m in saved)
    assert [m.content for m in model.prompts[-1][-3:]] == [
        "[当前问题]: 继续核对", "收到。", "[当前问题]: 第二条呢"]


def test_failed_summary_preserves_original_checkpoint_history(monkeypatch, summary_model):
    selected = runtime(InMemorySaver(), Capture())
    before = seed(selected)

    def unavailable(*args, **kwargs):
        raise SummaryUnavailable("provider unavailable")

    monkeypatch.setattr(compaction, "summarize", unavailable)
    run(selected, ChatRequest(message="继续"))
    saved = selected.store.load(config()).messages
    assert [m.id for m in saved[:len(before)]] == [m.id for m in before]
    assert not any(m.additional_kwargs.get("lc_source") for m in saved)


def test_summary_is_not_reused_after_role_change(summary_model):
    saver, model = InMemorySaver(), Capture()
    selected = runtime(saver, model)
    seed(selected)
    run(selected, ChatRequest(message="继续"))
    downgraded = Principal(7, "zako", SOURCE_ASSERTION)
    run(runtime(saver, model), ChatRequest(message="你好"), config(downgraded))
    assert "SUMMARY_CANARY" not in str(model.prompts[-1])


def test_host_summary_is_bootstrap_only_and_cannot_replace_saved_memory():
    saver, model = InMemorySaver(), Capture()
    scope = {"user_id": 7, "role": "user", "conversation_id": 42}
    run(runtime(saver, model), ChatRequest(message="你好", conversation_id=42,
                                        summary="BOOTSTRAP_CANARY",
                                        summary_scope=scope))
    assert "BOOTSTRAP_CANARY" in str(model.prompts[-1])
    run(runtime(saver, model), ChatRequest(message="继续", conversation_id=42,
                                        summary="LATE_HOST_CANARY",
                                        summary_scope=scope))
    assert "BOOTSTRAP_CANARY" in str(model.prompts[-1])
    assert "LATE_HOST_CANARY" not in str(model.prompts[-1])


def test_budgeted_summary_counts_once_and_rejects_incomplete_output():
    budget = RequestBudget(BudgetLimits(context_tokens=10000, request_tokens=20000))
    calls = []

    def invoke(prompt, config=None):
        calls.append(prompt)
        return AIMessage(content="summary", response_metadata={"finish_reason": "length"},
                         usage_metadata={"input_tokens": 100, "output_tokens": 12,
                                         "total_tokens": 112})

    with pytest.raises(SummaryUnavailable):
        summarize([HumanMessage(content="source")], budget=budget, config={},
                  model=SimpleNamespace(invoke=invoke))
    assert len(calls) == budget.model_calls == 1
    assert budget.tokens == 112


def test_compacted_model_node_resumes_without_repeating_summary(monkeypatch, summary_model):
    class Interrupted(Capture):
        def stream(self, messages, **kwargs):
            self.prompts.append(list(messages))
            if len(self.prompts) == 1:
                raise AgentCancelled()
            yield AIMessageChunk(content="continued")

    calls = []
    original = compaction.summarize

    def counted(*args, **kwargs):
        calls.append(1)
        return original(*args, **kwargs)

    monkeypatch.setattr(compaction, "summarize", counted)
    saver, model = InMemorySaver(), Interrupted()
    selected = runtime(saver, model)
    before = seed(selected)
    with pytest.raises(AgentCancelled):
        run(selected, ChatRequest(message="继续核对"))
    interrupted = selected.store.load(config())
    assert [m.id for m in interrupted.messages] == [m.id for m in before]
    reloaded = runtime(saver, model)
    list(reloaded.stream_events({"resume_task": interrupted.run["id"]}, config()))
    saved = reloaded.store.load(config()).messages
    assert len(calls) == 1
    assert len([m for m in saved if m.additional_kwargs.get("lc_source")]) == 1
    assert "SUMMARY_CANARY" in str(saved)
    assert not any("question-0:" in str(m.content) for m in saved)
    assert saved[-1].content == "continued"


def test_removed_empty_compatibility_channel_cannot_dispatch_an_old_payload():
    class PreviousState(ConversationState, total=False):
        continuation: dict

    saver = InMemorySaver()
    builder = StateGraph(PreviousState)
    builder.add_node("commit", lambda state: {})
    builder.add_edge(START, "commit")
    builder.add_edge("commit", END)
    builder.compile(checkpointer=saver).invoke({
        "messages": memory.turn_messages([HumanMessage(content="previous")], [], "reply", ACTOR),
        "continuation": {"payload": "OBSOLETE_EXECUTION_CANARY"}}, config())
    model = Capture()
    selected = runtime(saver, model)
    run(selected, ChatRequest(message="hello"))
    assert "OBSOLETE_EXECUTION_CANARY" not in str(model.prompts[-1])
    assert "continuation" not in selected.get_state(config()).values
    assert selected.store.load(config()).messages[-1].content == "收到。"
