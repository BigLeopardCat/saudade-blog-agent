"""Output headroom reaches the SDK and is charged by the existing task ledger."""

from types import SimpleNamespace

import pytest
from _framework_fixture import call, model_replies
from _own_support import config
from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import StructuredTool
from langgraph.checkpoint.memory import InMemorySaver

from agent.products.blog.application import BlogApplication
from agent.runtime import budget as budget_module
from agent.runtime import service
from agent.runtime.budget import BudgetLimits, TaskBudget, budget_scope
from agent.runtime.checkpoint_conversations import CheckpointConversationStore
from agent.runtime.output_budget import OutputBudget, material_tokens
from agent.runtime.session import ToolSession
from config.settings import settings


def wire_limit(request):
    return request.get("max_completion_tokens", request.get("max_tokens"))


def test_reading_evidence_expands_next_call_and_settles_actual_usage(monkeypatch):
    reservations = []
    monkeypatch.setattr(budget_module, "record", lambda node, event, **fields:
                        reservations.append(fields) if event == "reserved" else None)
    evidence = "可靠文章材料。" * 150
    reader = StructuredTool.from_function(lambda: evidence, name="fixture_read",
                                          description="Read source material.")
    with model_replies({"tool_calls": [call(reader.name)]},
                       {"content": "完整解释。"}) as (model, wire):
        session = ToolSession(model, config(), {reader.name: reader}, {},
                              output_budget=OutputBudget(512, 128))
        result = session.run([HumanMessage(content="读取文章并解释")])
    assert result.reply == "完整解释。"
    assert [wire_limit(row) for row in wire] == [128, 512]
    assert [row["output_limit"] for row in reservations] == [128, 512]
    assert any(row["role"] == "tool" and evidence in row["content"]
               for row in wire[1]["messages"])
    assert session.budget.model_calls == 2 and session.budget.tokens == 30


def test_completed_history_static_context_and_reasoning_do_not_expand_a_new_task():
    current = HumanMessage(content="你好", id="current")
    previous = [SystemMessage(content="x" * 20000), HumanMessage(content="旧问题", id="old"),
                ToolMessage(content="x" * 20000, tool_call_id="old-read"),
                AIMessage(content="x" * 20000,
                          additional_kwargs={"reasoning_content": "x" * 20000}),
                HumanMessage(content="x" * 20000, additional_kwargs={"memory_context": True})]
    assert material_tokens([*previous, current]) == material_tokens([current])
    with model_replies({"content": "你好。"}) as (model, wire):
        session = ToolSession(model, config(), {}, {}, output_budget=OutputBudget(512, 128))
        assert session.run([*previous, current]).reply == "你好。"
    assert wire_limit(wire[0]) == 128


def test_anchor_keeps_evidence_when_a_task_is_steered_or_resumed():
    request = HumanMessage(content="解释材料", id="task-start")
    evidence = ToolMessage(content="x" * 900, tool_call_id="read")
    steering = HumanMessage(content="改成列表", additional_kwargs={"task_input": {"id": "steer"}})
    messages = [request, evidence, steering]
    assert material_tokens(messages, "task-start") >= 600
    assert material_tokens(messages) >= 600
    assert material_tokens([*messages, HumanMessage(content="新任务", id="new")]) < 128


@pytest.mark.parametrize("ceiling", ["context_tokens", "task_tokens"])
def test_extended_cap_uses_only_available_room_without_dropping_current_material(ceiling):
    limits = BudgetLimits(**{ceiling: 1000})
    budget = TaskBudget(limits)
    material = "x" * 900
    with model_replies({"content": "完成。"}) as (model, wire), budget_scope(budget):
        session = ToolSession(model, config(), {}, {}, output_budget=OutputBudget(512, 128))
        assert session.run([HumanMessage(content=material)]).reply == "完成。"
    assert 128 < wire_limit(wire[0]) < 512
    assert any(row["content"] == material for row in wire[0]["messages"])
    assert budget.tokens == 15 and budget.reason is None


def test_optional_headroom_does_not_bypass_a_task_budget_stop():
    with model_replies({"content": "must not be sent"}) as (model, wire), \
            budget_scope(TaskBudget(BudgetLimits(task_tokens=300))):
        session = ToolSession(model, config(), {}, {}, output_budget=OutputBudget(512, 128))
        result = session.run([HumanMessage(content="x" * 900)])
    assert not wire
    assert result.execution["blocked"] == [{"reason": "task_tokens"}]


@pytest.mark.parametrize("override", ["model", "max_tokens", "max_completion_tokens"])
@pytest.mark.parametrize("adaptive", [False, True])
def test_outer_middleware_owns_its_explicit_output_limit(override, adaptive, monkeypatch):
    reservations = []
    monkeypatch.setattr(budget_module, "record", lambda node, event, **fields:
                        reservations.append(fields) if event == "reserved" else None)
    with model_replies({"content": "完成。"}) as (model, wire):
        class Override(AgentMiddleware):
            def wrap_model_call(self, request, handler):
                if override == "model":
                    selected = model.model_copy(update={"max_tokens": 64})
                    return handler(request.override(model=selected))
                return handler(request.override(model_settings={override: 64}))

        session = ToolSession(model, config(), {}, {}, middleware=[lambda _: Override()],
                              output_budget=OutputBudget(512, 128) if adaptive else None)
        assert session.run([HumanMessage(content="x" * 900)]).reply == "完成。"
    assert wire_limit(wire[0]) == 64 and session.budget.tokens == 15
    assert [row["output_limit"] for row in reservations] == [64]


def test_confirmation_and_background_responses_keep_the_base_cap():
    with model_replies() as (model, _):
        session = ToolSession(model, config(), {}, {}, output_budget=OutputBudget(512, 128))
        request = SimpleNamespace(model=model, model_settings={}, state={},
                                  messages=[HumanMessage(content="x" * 900)])
        limit, _ = session.output_budget.select(
            session, request, [HumanMessage(content="x" * 900)], concise=True)
    assert limit == 128


@pytest.mark.parametrize("custom_factory", [False, True])
def test_runtime_enables_policy_only_for_its_owned_model(monkeypatch, custom_factory):
    monkeypatch.setattr(settings, "agent_extended_output_tokens", 512)
    monkeypatch.setattr(settings, "agent_output_material_threshold", 128)
    with model_replies({"content": "完整解释。"}) as (model, wire):
        def factory(**_):
            return model
        monkeypatch.setattr(service, "get_llm", factory)
        runtime = service.AgentRuntime(CheckpointConversationStore(InMemorySaver()),
            application=BlogApplication(), model_factory=factory if custom_factory else None)
        result = list(runtime.stream_events(
            {"messages": [HumanMessage(content="x" * 900)]}, config()))
    assert result[-1].reply == "完整解释。"
    assert wire_limit(wire[0]) == (128 if custom_factory else 512)
