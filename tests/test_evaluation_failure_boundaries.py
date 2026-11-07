"""Fatal rounds retain evidence without spending on invalid conversational follow-ups."""

import json
from contextlib import contextmanager

import httpx
import pytest
from test_golden_rounds import case, response
from test_online_baseline import transport

from eval import golden_evaluation as golden
from eval import online_baseline


def test_fatal_http_round_preserves_receipts_and_never_sends_a_dependent_followup(
        tmp_path, monkeypatch):
    adapter = transport(tmp_path, monkeypatch,
        'event: receipts\ndata: [{"tool":"applied_write","result":"committed"}]\n\n'
        'event: text\ndata: "partial narration"\n\nevent: error\ndata: "unavailable"\n\n')
    sample = case([{"round": 1}, {"round": 2, "user_input": "continue"}])
    monkeypatch.setenv(golden.trace_mod.TOOL_RESULT_LIMIT_ENV, "8000")
    try:
        result = golden.run_case(sample, turn_runner=adapter)
    finally:
        adapter.client.close()
    assert len(adapter.requests) == len(result["rounds"]) == 1
    assert result["error"] and result["text"] == "partial narration"
    assert result["exec_rows"] == [{"tool": "applied_write", "result": "committed"}]
    frozen = list((tmp_path / "requests").glob("*.json"))
    assert len(frozen) == 1
    assert json.loads(frozen[0].read_text())["exec_rows"] == result["exec_rows"]
    assert golden.check_case(sample, result), "unexecuted follow-up is never a pass"


def test_business_unavailability_and_semantic_failure_allow_user_correction(monkeypatch):
    sent = []
    sample = case([{"round": 1, "gold": {"text_contains": ["expected"]}},
                   {"round": 2, "user_input": "corrected target"}])
    monkeypatch.setenv(golden.trace_mod.TOOL_RESULT_LIMIT_ENV, "8000")

    def runner(req, *_, **kwargs):
        sent.append(req.message)
        result = response("target unavailable" if len(sent) == 1 else "expected")
        result["native_events"] = [{"event": "call", "kind": "unavailable"}]
        return result

    result = golden.run_case(sample, turn_runner=runner)
    assert sent == ["first", "corrected target"] and len(result["rounds"]) == 2
    assert golden.check_case(sample, result), "later correction must not erase first-turn failure"


@pytest.mark.parametrize("facts", [
    {"failure_source": "provider", "http_status": 429, "retryable": True},
    {"failure_source": "provider", "http_status": 503, "retryable": True},
    {"failure_source": "transport", "error_type": "APIConnectionError", "retryable": True},
    {"failure_source": "provider", "http_status": 401, "retryable": False},
    {"failure_source": "provider", "http_status": 400, "retryable": False,
     "provider_error_code": "insufficient_quota"},
])
@pytest.mark.parametrize("phase", ["poc", "full"])
def test_captured_availability_failure_stops_paid_batch_and_keeps_partial_case(
        tmp_path, monkeypatch, facts, phase):
    trace = tmp_path / "trace.json"
    trace.write_text(json.dumps({"events": [{"event": "llm_error", **facts}]}))
    attempted = []
    result = response("partial")
    result.update(error="opaque failure", trace=str(trace), round=1)
    result["rounds"] = [dict(result)]
    cases = [{"id": str(index), "identity": "user", "tags": []} for index in range(2)]

    class Adapter:
        agent_url = "http://agent"
        requests = [{"trace": str(trace), "request_id": "attempted-once"}]
        cleaned_conversations = []

        def __init__(self):
            self.client = httpx.Client(transport=httpx.MockTransport(
                lambda request: httpx.Response(200, json={"sha": "frozen"}, request=request)))

        def profile(self, *_):
            return "user"

        @contextmanager
        def conversation(self, *_):
            yield 42

    def drive(sample, **kwargs):
        attempted.append(sample["id"])
        return result

    monkeypatch.setattr(online_baseline.golden, "run_case", drive)
    adapter = Adapter()
    try:
        report = online_baseline.run(cases, adapter, {"user": 7}, tmp_path / "report",
                                     phase=phase, suite_hash="synthetic")
    finally:
        adapter.client.close()
    assert attempted == ["0"] and report["counts"] == {"evaluation_error": 1}
    assert report["planned"] == 2 and not report["full_run"]
    assert report["cases"][0]["evaluation_failure"]["failure_source"] == facts["failure_source"]
    assert report["cases"][0]["result"]["text"] == "partial"
    assert report["requests"] == adapter.requests and report["automatic_retries"] == 0


@pytest.mark.parametrize("events,error", [
    ([{"event": "llm_error", "failure_source": "application", "http_status": 503,
       "retryable": True}], "model service unavailable"),
    ([{"event": "llm_error", "failure_source": "provider", "http_status": 400,
       "retryable": False}], "bad request"),
    ([{"event": "call", "kind": "unavailable"}], ""),
    ([{"event": "llm_error", "failure_source": "provider", "retryable": True},
      {"event": "llm_done"}], "later application error"),
])
def test_application_and_business_errors_are_not_invented_provider_failures(
        tmp_path, events, error):
    path = tmp_path / "trace.json"
    path.write_text(json.dumps({"events": events}))
    assert online_baseline.model_availability_failure({
        "rounds": [{"trace": str(path), "error": error}]}) is None
