"""Exercise the actual OpenEvals and LangChain SDK path with isolated HTTP responses."""

import json

import httpx
import pytest
from openai import LengthFinishReasonError, RateLimitError

from eval import online_judge, semantic_judge

VERDICT = {"unsupported": [], "answered": True, "verdict": "ok", "reason": "有依据"}


def evaluate(body, *, finish="stop", status=200, retries=0):
    calls = []

    def respond(request):
        calls.append(json.loads(request.content))
        if status != 200:
            return httpx.Response(status, json={"error": {"message": "unavailable"}})
        return httpx.Response(200, json={"id": "fixture", "model": "deepseek-v4.1-flash",
            "choices": [{"index": 0, "finish_reason": finish, "message": {
                "role": "assistant", "content": body}}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120,
                      "prompt_tokens_details": {"cached_tokens": 64}, "cost": 0,
                      "opencode": {"billing_source": "go", "list_cost": .001,
                                   "allowance": .0006}}})

    diagnostics = {}
    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        model = semantic_judge.make_llm(model="deepseek-v4.1-flash", api_key="fixture",
            base_url="https://model.test/v1", http_client=client, max_retries=retries)
        try:
            verdict = semantic_judge.judge_one("冻结材料", model,
                instructions=online_judge.INSTRUCTIONS, diagnostics=diagnostics)
            return verdict, diagnostics, calls
        except Exception as exc:
            exc.calls = calls
            exc.diagnostics = diagnostics
            raise


def test_standard_library_path_preserves_the_prompt_schema_usage_and_billing(monkeypatch):
    monkeypatch.setattr("config.settings.llm_provider", "opencode")
    got, diagnostics, calls = evaluate(json.dumps(VERDICT))
    assert got == VERDICT and len(calls) == 1
    assert calls[0]["response_format"] == {"type": "json_object"}
    assert calls[0]["messages"] == [
        {"role": "system", "content": online_judge.INSTRUCTIONS},
        {"role": "user", "content": "冻结材料"}]
    assert not calls[0].get("tools")
    assert diagnostics["usage"]["input_token_details"]["cache_read"] == 64
    assert diagnostics["billing"] == {"provider_billing_source": "go",
        "provider_cost_usd": 0, "provider_list_cost_usd": .001,
        "provider_allowance_usd": .0006}


@pytest.mark.parametrize("invalid", [
    "not json", "null", "[]",
    json.dumps({**VERDICT, "answered": "true"}),
    json.dumps({**VERDICT, "unsupported": "none"}),
    json.dumps({**VERDICT, "unsupported": ["x"] * 6}),
    json.dumps({**VERDICT, "unsupported": [1]}),
    json.dumps({**VERDICT, "reason": None}),
    json.dumps({**VERDICT, "unexpected": "value"}),
])
def test_invalid_output_never_retries_or_falls_back(invalid):
    with pytest.raises(ValueError) as caught:
        evaluate(invalid)
    assert len(caught.value.calls) == 1


@pytest.mark.parametrize("provider", ["opencode", "qwen"])
def test_length_finish_reason_is_unknown_even_with_valid_json(monkeypatch, provider):
    monkeypatch.setattr("config.settings.llm_provider", provider)
    with pytest.raises((ValueError, LengthFinishReasonError)) as caught:
        evaluate(json.dumps(VERDICT), finish="length")
    assert len(caught.value.calls) == 1
    assert caught.value.diagnostics["finish_reason"] == "length"


def test_rate_limit_remains_a_single_failed_request():
    with pytest.raises(RateLimitError) as caught:
        evaluate("", status=429)
    assert len(caught.value.calls) == 1


def test_retrying_clients_are_rejected_before_sending():
    with pytest.raises(ValueError, match="max_retries") as caught:
        evaluate(json.dumps(VERDICT), retries=2)
    assert caught.value.calls == []


def test_failed_answer_cannot_be_overridden_by_an_ok_summary():
    got, _, _ = evaluate(json.dumps({**VERDICT, "answered": False}))
    assert got["verdict"] == "suspect" and got["answered"] is False


def test_review_does_not_enable_langsmith_uploads(monkeypatch):
    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    requests = []
    monkeypatch.setattr("langsmith.client.Client.create_run", lambda *a, **kw: requests.append(kw))
    evaluate(json.dumps(VERDICT))
    assert requests == []


def test_explicit_reasoning_output_budget_preserves_defaults_and_sdk_options():
    model = semantic_judge.make_llm(reasoning_effort="low", max_tokens=8192,
                                   model="deepseek-v4.1-flash", api_key="fixture")
    assert semantic_judge.reviewer_options(model)["max_tokens"] == 8192
    assert semantic_judge.reviewer_options(model)["reasoning_effort"] == "low"
    default = semantic_judge.make_llm(model="deepseek-v4.1-flash", api_key="fixture")
    assert semantic_judge.reviewer_options(default)["max_tokens"] == 700
    assert semantic_judge.reviewer_options(default)["reasoning_effort"] == "none"


@pytest.mark.parametrize("budget", [0, -1, True, 1.5])
def test_invalid_judge_output_budget_is_rejected_before_client_construction(budget, monkeypatch):
    monkeypatch.setattr("models.llm.get_llm",
                        lambda **_: pytest.fail("invalid request constructed"))
    with pytest.raises(ValueError, match="positive integer"):
        semantic_judge.make_llm(max_tokens=budget)
