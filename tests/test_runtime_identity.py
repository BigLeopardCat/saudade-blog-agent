"""Running identity and actual reasoning metadata must not follow disk guesses."""

from types import SimpleNamespace

import pytest

from agent import runtime_probe
from agent.llm_usage import usage_fields
from agent.runtime.native import NativeRuntime
from agent.runtime.native_store import NativeConversationStore
from langgraph.checkpoint.memory import InMemorySaver
from models import llm


def test_version_stays_with_loaded_process_after_working_tree_changes(monkeypatch):
    before = runtime_probe.version_payload("test")
    monkeypatch.setattr(runtime_probe, "build_sha", lambda: ("changed", "git"))
    assert runtime_probe.version_payload("test") == before


def test_health_reports_actual_runtime_type():
    runtime = NativeRuntime(NativeConversationStore(InMemorySaver()))
    assert runtime_probe.arm_name(runtime) == "native"
    assert runtime_probe.arm_name(None) == "unknown"


@pytest.mark.parametrize("model,expected", [
    ("deepseek-v4.1-flash", {"thinking": {"type": "disabled"}}),
    ("minimax-m3.1-flash-free", {}), ("longcat-2.5-preview-free", {}),
])
def test_factory_sends_model_supported_thinking_control(monkeypatch, model, expected):
    monkeypatch.setattr(llm.settings, "llm_provider", "opencode")
    monkeypatch.setattr(llm, "ChatOpenAI", lambda **kwargs: kwargs)
    params = llm.get_llm(model=model, enable_thinking=False)
    assert params.get("extra_body", {}) == expected


def test_reasoning_usage_preserves_unknown_instead_of_assuming_zero():
    base = {"input_tokens": 10, "output_tokens": 3}
    def response(detail):
        return SimpleNamespace(usage_metadata={**base, "output_token_details": detail})
    assert usage_fields(response({"reasoning": 0}))["reasoning"] == 0
    assert usage_fields(response({"reasoning": 2}))["reasoning"] == 2
    assert "reasoning" not in usage_fields(response({}))
    raw = SimpleNamespace(response_metadata={"token_usage": {
        "prompt_tokens": 10, "completion_tokens": 3,
        "completion_tokens_details": {"reasoning_tokens": 2}}})
    assert usage_fields(raw)["reasoning"] == 2
