"""Provider hints constrain framework retries without exposing response payloads."""

import json
import secrets
import threading
import time
from email.utils import formatdate

import httpx
import pytest
from langchain_core.messages import AIMessage, HumanMessage
from openai import RateLimitError

from agent.runtime.control import RequestControl, request_scope
from agent.runtime.session import ToolSession
from models.errors import error_fields, retry_after


def rejected(headers=None):
    return RateLimitError("PRIVATE_EXCEPTION", response=httpx.Response(429,
        request=httpx.Request("POST", "https://model.test/PRIVATE_PATH"), headers=headers),
        body={"secret": secrets.token_urlsafe()})


@pytest.mark.parametrize("headers,expected", [
    ({"retry-after": "12"}, 12),
    ({"retry-after-ms": "1250", "retry-after": "9"}, 1.25),
    ({"retry-after": "-1"}, 0),
    ({"retry-after": "nonsense"}, None),
    ({"retry-after": "nan"}, None),
    ({"retry-after": "inf"}, None),
])
def test_retry_after_is_bounded_by_http_semantics(headers, expected):
    assert retry_after(rejected(headers)) == expected


def test_http_date_uses_utc_and_records_only_safe_diagnostic_headers(monkeypatch):
    monkeypatch.setattr("models.errors.time.time", lambda: 1000)
    fields = error_fields(rejected({"retry-after": formatdate(1010, usegmt=True),
        "x-request-id": "provider_123", "authorization": "PRIVATE_HEADER"}))
    assert fields["retry_after_s"] == 10
    assert fields["provider_request_id"] == "provider_123"
    from eval import request_metrics
    metrics = request_metrics.summary({"events": [{"event": "llm_error", **fields}]})
    assert metrics["model_failures"] == [fields]
    assert "PRIVATE" not in json.dumps(fields)
    assert "provider_request_id" not in error_fields(rejected({"x-request-id": "https://private.test"}))


@pytest.mark.parametrize("headers,remaining,attempts", [({}, 30, 2),
    ({"retry-after": "10"}, 30, 1), ({"retry-after-ms": "1000"}, 30, 2),
    ({"retry-after": "2"}, 30, 2), ({"retry-after": "3"}, 30, 1),
    ({}, 1, 1)])
def test_framework_does_not_retry_early_or_beyond_remaining_deadline(
        monkeypatch, headers, remaining, attempts):
    calls, sleeps = [], []
    monkeypatch.setattr("langchain.agents.middleware.model_retry.time.sleep", sleeps.append)

    class Model:
        def bind_tools(self, *args, **kwargs):
            return self

        def invoke(self, *args, **kwargs):
            calls.append(1)
            raise rejected(headers)

    stopped = threading.Event()
    with request_scope(RequestControl(stopped, time.monotonic() + remaining)):
        session = ToolSession(Model(), {}, {}, {})
        with pytest.raises(RateLimitError):
            session.run([HumanMessage(content="Synthetic request")])
    assert len(calls) == session.budget.model_calls == attempts
    assert len(sleeps) == attempts - 1
    if sleeps:
        assert sleeps == [2.0]
        assert sleeps[0] >= (retry_after(rejected(headers)) or 0)


def test_provider_two_second_hint_recovers_once_with_all_attempts_charged(monkeypatch):
    calls, sleeps = [], []
    monkeypatch.setattr("langchain.agents.middleware.model_retry.time.sleep", sleeps.append)

    class Model:
        def bind_tools(self, *args, **kwargs):
            return self

        def invoke(self, *args, **kwargs):
            calls.append(1)
            if len(calls) == 1:
                raise rejected({"retry-after": "2"})
            return AIMessage(content="Synthetic response")

    session = ToolSession(Model(), {}, {}, {})
    result = session.run([HumanMessage(content="Synthetic request")])
    assert result.reply == "Synthetic response"
    assert sleeps == [2.0] and len(calls) == session.budget.model_calls == 2


def test_cancelling_during_framework_backoff_does_not_make_another_model_request(monkeypatch):
    from agent.runtime.control import AgentCancelled

    stopped, calls = threading.Event(), []
    monkeypatch.setattr("langchain.agents.middleware.model_retry.time.sleep",
                        lambda _: stopped.set())

    class Model:
        def bind_tools(self, *args, **kwargs):
            return self

        def invoke(self, *args, **kwargs):
            calls.append(1)
            raise rejected()

    session = ToolSession(Model(), {"configurable": {"stop_event": stopped}}, {}, {})
    with pytest.raises(AgentCancelled):
        session.run([HumanMessage(content="Synthetic request")])
    assert calls == [1]


def test_api_failure_messages_are_safe_and_do_not_claim_persistence():
    from agent.api.routes.chat import PRODUCER_ERROR_TEXT, _producer_error_text

    text = _producer_error_text(rejected({"retry-after": "15"}))
    assert "15" in text and "PRIVATE" not in text
    assert "已保存" not in text
    assert _producer_error_text(ValueError("PRIVATE")) == PRODUCER_ERROR_TEXT


def test_explicit_quota_failure_is_not_retried_or_misreported_as_task_quality_failure(tmp_path):
    from agent.api.routes.chat import _producer_error_text
    from eval import online_baseline
    from models.errors import retryable_model_request

    error = RateLimitError("PRIVATE", response=httpx.Response(429,
        request=httpx.Request("POST", "https://model.test")),
        body={"type": "insufficient_quota", "code": "insufficient_quota"})
    fields = error_fields(error)
    assert fields["retryable"] is False and not retryable_model_request(error)
    assert "额度" in _producer_error_text(error) and "PRIVATE" not in _producer_error_text(error)
    trace = tmp_path / "quota.json"
    trace.write_text(json.dumps({"events": [{"event": "llm_error", **fields}]}))
    assert online_baseline.model_availability_failure({"rounds": [
        {"trace": str(trace), "error": "unavailable"}]})


def test_malformed_provider_codes_are_not_serialized_or_allowed_to_break_diagnostics():
    error = RateLimitError("PRIVATE", response=httpx.Response(429,
        request=httpx.Request("POST", "https://model.test")),
        body={"type": ["PRIVATE"], "code": {"secret": secrets.token_urlsafe()}})
    fields = error_fields(error)
    assert fields["http_status"] == 429 and "PRIVATE" not in json.dumps(fields)
