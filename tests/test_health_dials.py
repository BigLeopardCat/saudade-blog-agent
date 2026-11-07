"""Health reports live resources and settings without exposing credentials."""

import asyncio
import json
from types import SimpleNamespace

import pytest
from _service import app, service_request
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

from agent import runtime_probe
from agent.api.routes import operations
from config.settings import settings

DIAL_KEYS = {"runtime", "budget", "llm_provider", "llm_model", "rag_hybrid_enabled",
             "rag_hybrid_active", "rag_state", "vector_missing", "checkpoint_backend",
             "checkpoint_db"}
BUDGET_KEYS = {"context_tokens", "input_tokens", "task_tokens", "task_seconds",
               "model_calls", "tool_calls", "output_tokens", "summary_output_tokens"}


def health():
    return asyncio.run(operations.health(service_request()))


@pytest.mark.parametrize("runtime,ready", [(None, False), (object(), True)])
def test_health_shape_and_liveness_reflect_installed_runtime(monkeypatch, runtime, ready):
    monkeypatch.setattr(app.state, "runtime", runtime)
    response = health()
    assert set(response) == {"status", "agent_ready", "dials", "model_transport"}
    assert response["status"] == "ok" and response["agent_ready"] is ready
    assert response["model_transport"] is None
    assert set(response["dials"]) == DIAL_KEYS
    wire = json.dumps(response, separators=(",", ":"))
    assert ('"agent_ready":true' in wire) is ready


def test_diagnostics_use_effective_settings_instead_of_reloading_environment(monkeypatch):
    monkeypatch.setattr(settings, "agent_output_tokens", 2048)
    monkeypatch.setenv("AGENT_OUTPUT_TOKENS", "4096")
    dials = health()["dials"]
    assert dials["budget"]["output_tokens"] == 2048
    assert set(dials["budget"]) == BUDGET_KEYS
    assert all(type(v) is int and v > 0 for v in dials["budget"].values())
    assert dials["llm_model"] == settings.active_llm_model


def test_invalid_provider_does_not_break_liveness_or_invent_a_model(monkeypatch):
    previous = settings.llm_provider
    monkeypatch.setattr(settings, "llm_provider", "SENTINEL_PROVIDER")
    response = health()
    assert response["status"] == "ok"
    assert response["dials"]["llm_provider"] == "SENTINEL_PROVIDER"
    assert "认不出" in response["dials"]["llm_model"]
    monkeypatch.setattr(settings, "llm_provider", previous)
    assert health()["dials"]["llm_model"] == settings.active_llm_model


def test_diagnostics_exclude_credentials_and_arbitrary_nested_settings(monkeypatch):
    canary = "sk-SENTINEL-MUST-NOT-LEAK-0123456789"
    for key in ("confirmation_secret", "qwen_api_key", "deepseek_api_key", "openai_api_key"):
        monkeypatch.setattr(settings, key, canary)
    monkeypatch.setattr(settings, "llm_provider", "qwen")
    response = health()
    assert canary not in json.dumps(response)
    dials = response["dials"]
    assert set(dials) == DIAL_KEYS
    for key, value in dials.items():
        if key != "budget":
            assert (isinstance(value, (str, bool))
                    or (key == "vector_missing" and type(value) is int))
            assert len(str(value)) <= 64


@pytest.mark.parametrize("enabled,state", [(True, "missing_credentials"), (False, "off")])
def test_rag_distinguishes_configured_enabled_and_actually_active(monkeypatch, enabled, state):
    monkeypatch.setattr(settings, "rag_hybrid_enabled", enabled)
    monkeypatch.setattr(settings, "embedding_api_key", "")
    monkeypatch.setattr(settings, "embedding_model", "")
    dials = health()["dials"]
    assert dials["rag_hybrid_enabled"] is enabled
    assert dials["rag_hybrid_active"] is False
    assert dials["rag_state"] == state


def async_saver():
    async def create():
        return AsyncPostgresSaver(None)
    return asyncio.run(create())


@pytest.mark.parametrize("runtime,expected", [
    (None, "unknown"), (object(), "unknown"),
    (SimpleNamespace(checkpointer=InMemorySaver()), "unknown"),
    (SimpleNamespace(graph=SimpleNamespace(saver=InMemorySaver())), "unknown"),
    (SimpleNamespace(saver=None, graph=SimpleNamespace(saver=InMemorySaver())), "none"),
    (SimpleNamespace(saver=InMemorySaver()), "memory"),
    (SimpleNamespace(saver=PostgresSaver(None)), "postgres"),
    (SimpleNamespace(saver=async_saver()), "postgres"),
    (SimpleNamespace(saver=type("UnknownSaver", (), {"__module__": "my.custom.saver"})()),
     "my.custom.saver"),
])
def test_checkpoint_backend_uses_public_runtime_saver_contract(runtime, expected):
    assert runtime_probe.checkpoint_backend(runtime) == expected


@pytest.mark.parametrize("dsn,expected", [
    ("", ""), ("   ", ""),
    ("postgresql://u:secret@10.0.0.9:5432/ckpt_db", "ckpt_db"),
    ("postgresql://u:p@h:5432/ckpt_db?sslmode=require", "ckpt_db"),
    ("postgresql:///ckpt_db", "ckpt_db"),
    ("postgresql://u:p@h:5432", "unknown"), ("postgresql://", "unknown"),
])
def test_checkpoint_database_name_never_exposes_credentials_or_host(dsn, expected):
    result = runtime_probe.checkpoint_db(dsn)
    assert result == expected
    assert "secret" not in result and "10.0.0.9" not in result


def test_health_separates_configured_database_from_actual_checkpoint_backend(monkeypatch):
    monkeypatch.setattr(app.state, "runtime", SimpleNamespace(saver=PostgresSaver(None)))
    assert health()["dials"]["checkpoint_backend"] == "postgres"
    monkeypatch.setattr(app.state, "runtime", SimpleNamespace(saver=None))
    for dsn, name in (("postgresql://u:p@h:5432/from_settings", "from_settings"), ("", "")):
        monkeypatch.setattr(settings, "checkpoint_dsn", dsn)
        dials = health()["dials"]
        assert dials["checkpoint_backend"] == "none"
        assert dials["checkpoint_db"] == name
