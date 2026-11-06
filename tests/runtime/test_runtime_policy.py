"""Production budget knobs reach the real loop, transport and summary boundary."""

from types import SimpleNamespace

import pytest
from _own_support import config
from langchain_core.messages import AIMessage, HumanMessage
from pydantic import ValidationError

from agent.products.blog.application import BlogApplication
from agent.runtime.budget import BudgetLimits, TaskBudget, budget_scope
from agent.runtime.checkpoint_conversations import CheckpointConversationStore
from agent.runtime.service import AgentRuntime
from agent.runtime.session import ToolSession
from config.settings import Settings, settings


def test_settings_supply_actual_request_limits(monkeypatch):
    values = dict(agent_context_tokens=6000, agent_task_tokens=9000,
                  agent_model_calls=3, agent_tool_calls=2, agent_output_tokens=320,
                  agent_summary_output_tokens=256)
    policy = Settings(_env_file=None, **values)
    for key in values:
        monkeypatch.setattr(settings, key, getattr(policy, key))
    budget = TaskBudget()
    assert budget.limits == BudgetLimits(context_tokens=6000, task_tokens=9000,
                                        model_calls=3, tool_calls=2)
    ticket = budget.reserve([HumanMessage(content="hello")])
    assert ticket.output_limit == 320
    assert budget.tokens == ticket.estimated_input + 320


@pytest.mark.parametrize("values", [
    {"agent_context_tokens": 0}, {"agent_task_tokens": -1},
    {"agent_model_calls": 1}, {"agent_tool_calls": 0},
    {"agent_output_tokens": 0}, {"agent_summary_output_tokens": 0},
    {"agent_context_tokens": 512}, {"agent_task_tokens": 512},
])
def test_invalid_budget_settings_fail_before_runtime_start(values):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **values)


@pytest.mark.parametrize("name", ["graph", "hybrid", "react", "unknown", "native"])
def test_retired_selection_rejected_from_environment_dotenv_and_constructor(
        monkeypatch, tmp_path, name):
    monkeypatch.setenv("AGENT_ARM", name)
    with pytest.raises(ValidationError, match="retired"):
        Settings(_env_file=None)
    monkeypatch.delenv("AGENT_ARM")
    dotenv = tmp_path / ".env"
    dotenv.write_text(f"AGENT_ARM={name}\n", encoding="utf-8")
    with pytest.raises(ValidationError, match="retired"):
        Settings(_env_file=dotenv)
    with pytest.raises(ValidationError, match="retired"):
        Settings(_env_file=None, agent_arm=name)


def test_runtime_configuration_has_no_experimental_selector():
    policy = Settings(_env_file=None)
    retired = {"agent_arm", "recursion_limit", "planner_native_thinking", "agent_fastpath"}
    assert not retired.intersection(type(policy).model_fields)


def test_runtime_model_uses_configured_output_without_changing_sdk_default(monkeypatch):
    calls = []
    monkeypatch.setattr(settings, "agent_output_tokens", 1792)
    monkeypatch.setattr(settings, "llm_max_tokens", 8192)

    def factory(**kwargs):
        calls.append(kwargs)
        model = SimpleNamespace(max_tokens=kwargs["max_tokens"])
        model.bind_tools = lambda *a, **k: model
        return model

    runtime = AgentRuntime(
        CheckpointConversationStore(None), application=BlogApplication(), model_factory=factory
    )
    # Construct the same session used by ordinary native turns.
    runtime._session(config(), runtime.store.load(config()))
    assert calls[0]["max_tokens"] == 1792
    assert calls[0]["enable_thinking"] is False and calls[0]["streaming"] is True
    assert settings.llm_max_tokens == 8192


class Model:
    max_tokens = 64

    def invoke(self, prompt):
        return AIMessage(content="complete")


def test_full_token_budget_replaces_the_unrelated_prompt_character_cap():
    prompt = [HumanMessage(content="x" * 25000)]
    with budget_scope(TaskBudget(BudgetLimits(context_tokens=20000, task_tokens=40000))):
        assert ToolSession(Model(), {}, {}, {}).run(prompt).reply == "complete"
    with budget_scope(TaskBudget(BudgetLimits(context_tokens=1000))):
        session = ToolSession(Model(), {}, {}, {})
        result = session.run(prompt)
        assert session.budget.reason == "context_tokens"
        assert session.budget.model_calls == 0 and result.reply != "complete"


