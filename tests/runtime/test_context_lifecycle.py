"""Conversation correctness across compaction, storage and task recovery."""

import pytest
from _memory_support import ACTOR, Capture, config, run, runtime
from langchain_core.language_models.fake_chat_models import FakeListChatModel
from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage
from langgraph.checkpoint.memory import InMemorySaver

from agent import checkpoint
from agent.api.schemas import ChatRequest
from agent.principal import SOURCE_ASSERTION, Principal
from agent.runtime import compaction, memory, retention
from agent.runtime.budget import BudgetExceeded, BudgetLimits, RequestBudget, budget_scope
from agent.runtime.summarization import SummaryUnavailable
from agent.runtime.task_budget import for_saver
from agent.runtime.turns import AgentCancelled
from config.settings import settings


def test_long_latest_turn_reaches_summarizer_before_any_history_is_removed(monkeypatch):
    monkeypatch.setattr(settings, "agent_context_tokens", 8000)
    monkeypatch.setattr(settings, "agent_summary_output_tokens", 512)
    monkeypatch.setattr("models.get_llm", lambda **_: FakeListChatModel(responses=["unused"]))
    selected, sources = runtime(InMemorySaver(), Capture()), []
    old = memory.turn_messages([HumanMessage(content="项目代号 ONLY_EARLY_FACT")], [],
                               "明白", ACTOR)
    long = memory.turn_messages([HumanMessage(content="长文材料" * 2200)], [], "已读", ACTOR)
    selected.store.update_state(config(), {"messages": [*old, *long]})

    def summarize(messages, **kwargs):
        sources.extend(messages)
        return "项目代号 ONLY_EARLY_FACT。"

    monkeypatch.setattr(compaction, "summarize", summarize)
    run(selected, ChatRequest(message="项目代号是什么？"))
    assert "ONLY_EARLY_FACT" in str(sources)
    saved = selected.store.load(config()).messages
    assert any(m.additional_kwargs.get("lc_source") == "summarization" for m in saved)
    assert "ONLY_EARLY_FACT" in str(selected.model_factory().prompts[-1])


def test_failed_summary_can_commit_history_larger_than_the_model_window(monkeypatch):
    monkeypatch.setattr(settings, "agent_context_tokens", 16384)
    monkeypatch.setattr("models.get_llm", lambda **_: FakeListChatModel(responses=["unused"]))
    model = Capture()
    selected = runtime(InMemorySaver(), model)
    before = []
    for index in range(20):
        before.extend(memory.turn_messages([HumanMessage(content=f"第{index}段：" + "历史" * 550)],
                                           [], "收到", ACTOR))
    selected.store.update_state(config(), {"messages": before})
    monkeypatch.setattr(compaction, "summarize", lambda *a, **k: (_ for _ in ()).throw(
        SummaryUnavailable("synthetic unavailable")))
    result = run(selected, ChatRequest(message="本轮" * 1500))[-1]
    saved = selected.store.load(config()).messages
    assert result.reply == saved[-1].content == "收到。"
    assert [m.id for m in saved[:len(before)]] == [m.id for m in before]
    assert saved[-2].content == "[当前问题]: " + "本轮" * 1500
    assert memory._tokens(saved) > selected.model_factory().max_tokens + 16384
    assert memory._tokens(model.prompts[-1]) < 16384
    assert not selected.store.load(config()).unfinished


@pytest.mark.parametrize("actor", [ACTOR, Principal(999, "zako", SOURCE_ASSERTION)])
def test_unknown_checkpoint_origin_cannot_be_dated_by_the_current_caller(actor):
    unknown = [HumanMessage(content="unknown"), AIMessage(content="PRIVATE_CANARY")]
    assert memory.history(unknown, actor) == []
    assert memory.history(memory.turn_messages([unknown[0]], [], "known", ACTOR), ACTOR)


def test_retention_evicts_whole_turns_only_at_the_independent_storage_ceiling(monkeypatch):
    messages = []
    for index in range(3):
        messages.extend(memory.turn_messages([HumanMessage(content=f"question-{index}")], [],
                                            f"reply-{index}", ACTOR))
    import json

    from langchain_core.messages import messages_to_dict

    monkeypatch.setattr(retention, "MAX_BYTES", len(json.dumps(
        messages_to_dict(messages[2:]), ensure_ascii=False).encode()) + 4)
    kept = retention.retain(messages)
    assert kept[0].additional_kwargs["memory_retention"]
    assert [m.content for m in kept[1:]] == [m.content for m in messages[2:]]
    assert "question-0" not in str(kept)
    assert retention.retain(kept) == kept
    assert memory.history(kept, Principal(8, "user", SOURCE_ASSERTION)) == []


def test_reconstructed_budget_retains_reservation_and_settles_actual_usage():
    saver = InMemorySaver()
    storage = for_saver(saver)
    task = {"id": "budget-task", "scope": {"uid": 7, "role": "user", "conversation_id": 42}}
    first = RequestBudget(BudgetLimits(model_calls=2))
    first.bind_task(storage, task)
    ticket = first.reserve([HumanMessage(content="request")], output_limit=64)
    assert storage.load(task)["model_calls"] == 1
    first.settle(ticket, AIMessage(content="answer", usage_metadata={
        "input_tokens": 10, "output_tokens": 5, "total_tokens": 15}))
    restored = RequestBudget(BudgetLimits(model_calls=2))
    restored.bind_task(for_saver(saver), task)
    assert restored.tokens == 15 and restored.model_calls == 1
    restored.reserve([HumanMessage(content="retry")], output_limit=64)
    retry = RequestBudget(BudgetLimits(model_calls=2))
    retry.bind_task(for_saver(saver), task)
    with pytest.raises(BudgetExceeded, match="model_calls"):
        retry.reserve([HumanMessage(content="third")], output_limit=64)
    with pytest.raises(ValueError, match="scope"):
        for_saver(saver).load({**task, "scope": {**task["scope"], "uid": 8}})
    assert checkpoint.delete_thread(saver, 42)
    assert for_saver(saver).rows == {}
    with pytest.raises(ValueError, match="unavailable"):
        RequestBudget().bind_task(for_saver(saver), task, create=False)


def test_cancelled_model_attempt_counts_against_the_same_resumed_task():
    class Interrupted(Capture):
        def stream(self, messages, **kwargs):
            self.prompts.append(list(messages))
            if len(self.prompts) == 1:
                raise AgentCancelled()
            yield AIMessageChunk(content="continued")

    saver, model = InMemorySaver(), Interrupted()
    first = RequestBudget(BudgetLimits(model_calls=2))
    with budget_scope(first), pytest.raises(AgentCancelled):
        run(runtime(saver, model), ChatRequest(message="继续任务"))
    selected = runtime(saver, model)
    task = selected.store.load(config()).run
    second = RequestBudget(BudgetLimits(model_calls=2))
    with budget_scope(second):
        result = list(selected.stream_events({"resume_task": task["id"]}, config()))[-1]
    assert result.reply == "continued"
    assert second.model_calls == 2 and len(model.prompts) == 2
    assert second.tokens >= first.tokens


def test_resume_cannot_exceed_an_exhausted_task_model_limit():
    from langchain_core.tools import tool

    from agent.products.blog.capabilities import ToolCatalog
    from agent.runtime.native import NativeRuntime
    from agent.runtime.native_store import NativeConversationStore
    from tools.result import ok

    @tool
    def read_fixture() -> object:
        """Read a local synthetic observation."""
        return ok("fixture fact")

    class Interrupted(Capture):
        def stream(self, messages, **kwargs):
            self.prompts.append(list(messages))
            if len(self.prompts) == 1:
                yield AIMessageChunk(content="", tool_calls=[{
                    "name": "read_fixture", "args": {}, "id": "read"}])
            else:
                raise AgentCancelled()

    model, saver = Interrupted(), InMemorySaver()
    catalog = ToolCatalog(read_tools={"read_fixture": read_fixture})

    def selected():
        return NativeRuntime(NativeConversationStore(saver), catalog=catalog,
                             model_factory=lambda **_: model)

    with budget_scope(RequestBudget(BudgetLimits(model_calls=2))), pytest.raises(AgentCancelled):
        run(selected(), ChatRequest(message="读取并解释"))
    task = selected().store.load(config()).run
    resumed = RequestBudget(BudgetLimits(model_calls=2))
    with budget_scope(resumed):
        result = list(selected().stream_events({"resume_task": task["id"]}, config()))[-1]
    assert resumed.model_calls == len(model.prompts) == 2
    assert resumed.request_spent()["model_calls"] == 0
    assert "达到调用或上下文上限" in result.reply


def test_byte_retention_preserves_the_latest_complete_turns(monkeypatch):
    import json

    from langchain_core.messages import messages_to_dict

    monkeypatch.setattr(retention, "MAX_BYTES", 8000)
    messages = []
    for index in range(3):
        messages.extend(memory.turn_messages(
            [HumanMessage(content=f"question-{index}: " + "记忆" * 500)], [],
            f"reply-{index}", ACTOR))
    kept = retention.retain(messages)
    assert kept[0].additional_kwargs["memory_retention"]
    assert len(kept) == 5
    assert [m.content for m in kept[-2:]] == [m.content for m in messages[-2:]]
    assert len(json.dumps(messages_to_dict(kept), ensure_ascii=False).encode()) <= 8000
