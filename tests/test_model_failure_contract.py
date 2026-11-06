"""Model availability facts stay safe, typed and separate from task-quality failures."""

import asyncio
import hashlib
import json
import secrets
import time
from contextlib import contextmanager

import httpx
import pytest
from langchain_core.messages import HumanMessage
from openai import APIConnectionError, BadRequestError, RateLimitError

from agent.runtime.session import ToolSession
from eval import online_baseline, request_metrics
from models.errors import error_fields


def overloaded():
    return RateLimitError("PRIVATE_UPSTREAM_CANARY", response=httpx.Response(
        429, request=httpx.Request("POST", "https://unit.invalid")),
        body={"secret": secrets.token_urlsafe()})


@pytest.mark.parametrize("error,expected", [
    (overloaded(), {"failure_source": "provider", "http_status": 429, "retryable": True}),
    (BadRequestError("private", response=httpx.Response(400,
        request=httpx.Request("POST", "https://unit.invalid")), body=None),
        {"failure_source": "provider", "http_status": 400, "retryable": False}),
    (APIConnectionError(request=httpx.Request("POST", "https://unit.invalid")),
        {"failure_source": "transport", "retryable": True}),
    (ValueError("PRIVATE_ARGUMENT_CANARY"),
        {"failure_source": "application", "retryable": False}),
])
def test_failure_facts_do_not_include_exception_messages_requests_or_response_bodies(
    error, expected
):
    assert error_fields(error) == {"error_type": type(error).__name__, **expected}
    assert "PRIVATE" not in json.dumps(error_fields(error))
    assert "secret" not in json.dumps(error_fields(error))


def test_each_failed_framework_attempt_has_safe_status_and_no_invented_usage(monkeypatch):
    events = []
    monkeypatch.setattr("agent.runtime.middleware.record", lambda node, event, **data:
                        events.append({"node": node, "event": event, **data}))

    class UnavailableModel:
        def bind_tools(self, *args, **kwargs):
            return self

        def invoke(self, messages, **kwargs):
            raise overloaded()

    session = ToolSession(UnavailableModel(), {}, {}, {})
    monkeypatch.setattr("langchain.agents.middleware.model_retry.time.sleep", lambda _: None)
    with pytest.raises(RateLimitError):
        list(session.stream([HumanMessage(content="hello")]))
    failed = [event for event in events if event["event"] == "llm_error"]
    assert len(failed) == 2 and all(event["http_status"] == 429 for event in failed)
    assert failed[0]["failure_source"] == "provider" and failed[0]["retryable"] is True
    assert "input" not in failed[0] and "cache_read" not in failed[0]
    assert "PRIVATE_UPSTREAM_CANARY" not in json.dumps(events)


def test_framework_retry_recovers_once_and_accounts_each_attempt(monkeypatch):
    from langchain_core.messages import AIMessage

    events, calls = [], []
    monkeypatch.setattr("langchain.agents.middleware.model_retry.time.sleep", lambda _: None)
    monkeypatch.setattr("agent.runtime.middleware.record", lambda node, event, **data:
                        events.append({"node": node, "event": event, **data}))

    class TemporaryOverload:
        def bind_tools(self, *args, **kwargs):
            return self

        def invoke(self, messages, **kwargs):
            calls.append(1)
            if len(calls) == 1:
                raise overloaded()
            return AIMessage(content="recovered answer")

    session = ToolSession(TemporaryOverload(), {}, {}, {})
    terminal = session.run([HumanMessage(content="hello")])
    assert terminal.reply == "recovered answer" and len(calls) == 2
    assert session.budget.model_calls == 2
    assert sum(e["event"] == "llm_start" for e in events) == 2
    assert sum(e["event"] == "llm_error" for e in events) == 1
    assert sum(e["event"] == "llm_done" for e in events) == 1
    assert request_metrics.summary({"events": events})["model_retry_attempts"] == 1


def test_summary_failure_has_the_same_sdk_facts_and_keeps_source_messages(monkeypatch):
    from agent.runtime.budget import TaskBudget
    from agent.runtime.summarization import summarize

    events = []
    monkeypatch.setattr("agent.runtime.summarization.record", lambda node, event, **data:
                        events.append({"node": node, "event": event, **data}))

    class FailedSummary:
        def invoke(self, *args, **kwargs):
            raise overloaded()

    source = [HumanMessage(content="retained source")]
    with pytest.raises(RateLimitError):
        summarize(source, budget=TaskBudget(), config={}, model=FailedSummary())
    assert source[0].content == "retained source"
    assert [e["event"] for e in events] == ["llm_start", "llm_error"]
    assert events[-1]["http_status"] == 429 and events[-1]["retryable"] is True


def test_model_response_timing_does_not_record_headers_or_error_payload(monkeypatch):
    from models.transport import _track_response

    events = []
    monkeypatch.setattr("models.transport.record", lambda node, event, **data:
                        events.append({"node": node, "event": event, **data}))
    request = httpx.Request("POST", "https://unit.invalid/PRIVATE_PATH",
                            headers={"authorization": "Bearer PRIVATE_SECRET"},
                            extensions={"saudade_model_started": time.monotonic() - 1})
    response = httpx.Response(429, request=request, json={"error": "PRIVATE_PAYLOAD"})
    asyncio.run(_track_response(response))
    assert len(events) == 1 and events[0]["http_status"] == 429
    assert events[0]["event"] == "http_response_headers" and events[0]["duration_s"] >= 1
    assert "PRIVATE" not in json.dumps(events)


@pytest.mark.parametrize("recovered_index", [None, 2])
def test_availability_stop_uses_captured_sdk_facts_without_resampling(
        tmp_path, monkeypatch, recovered_index):
    path = tmp_path / "failed-trace.json"
    path.write_text(json.dumps({"events": [{"node": "native", "event": "llm_error",
        "http_status": 429, "failure_source": "provider", "retryable": True,
        "error_type": "RateLimitError"}]}))
    result = {"rounds": [{"trace": str(path), "error": "unavailable"}]}
    assert online_baseline.provider_unavailable({"result": result})
    assert not online_baseline.provider_unavailable({"result": {"rounds": [{
        "text": "upstream 429 overloaded retryable true", "error": "429"}]}})
    recovered_path = tmp_path / "recovered-trace.json"
    recovered_path.write_text(json.dumps({"events": [
        {"event": "llm_error", "failure_source": "provider", "retryable": True},
        {"event": "llm_done", "input": 20, "output": 5, "cache_read": 0}]}))
    recovered = {"rounds": [{"trace": str(recovered_path), "error": ""}]}
    assert not online_baseline.provider_unavailable({"result": recovered})
    assert not online_baseline.provider_unavailable({"result": {"rounds": [
        {"trace": str(recovered_path), "error": "later application failure"}]}})
    calls = []

    class Fake:
        agent_url = "http://agent"
        requests, cleaned_conversations = [], []
        client = httpx.Client(transport=httpx.MockTransport(lambda request:
            httpx.Response(200, json={"sha": "frozen"}, request=request)))

        def profile(self, *_):
            return "user"

        @contextmanager
        def conversation(self, *_):
            yield 1

    def drive(case, **kwargs):
        calls.append(case["id"])
        return recovered if case["id"] == str(recovered_index) else result

    monkeypatch.setattr(online_baseline.golden, "run_case", drive)
    monkeypatch.setattr(online_baseline.golden, "check_case", lambda case, row:
                        [] if row is recovered else ["model unavailable"])
    cases = [{"id": str(i), "identity": "user", "tags": []} for i in range(8)]
    transport = Fake()
    try:
        report = online_baseline.run(cases, transport, {"user": 7}, tmp_path / "report",
                                     phase="full", suite_hash="synthetic")
    finally:
        transport.client.close()
    expected = 3 if recovered_index is None else 6
    assert calls == [str(i) for i in range(expected)]
    assert report["counts"] == ({"failed": 3} if recovered_index is None else
                                {"failed": 5, "passed": 1})
    assert not report["full_run"] and "availability" in report["stopped_reason"]
    assert report["automatic_retries"] == 0
    frozen = tmp_path / "report" / "suite.jsonl"
    assert [json.loads(line) for line in frozen.read_text().splitlines()] == cases
    assert frozen.stat().st_mode & 0o777 == 0o600
    assert report["selected_suite_sha256"] == hashlib.sha256(frozen.read_bytes()).hexdigest()
    metrics = request_metrics.summary(json.loads(path.read_text()))
    assert metrics["model_failures"] == [{"error_type": "RateLimitError",
        "failure_source": "provider", "http_status": 429, "retryable": True}]
