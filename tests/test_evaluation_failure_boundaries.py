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
    {"failure_source": "transport", "end_reason": "client_disconnect"},
])
@pytest.mark.parametrize("phase", ["poc", "full"])
def test_captured_availability_failure_stops_paid_batch_and_keeps_partial_case(
        tmp_path, monkeypatch, facts, phase):
    trace = tmp_path / "trace.json"
    trace.write_text(json.dumps({"end_reason": facts.get("end_reason"),
                                "events": [{"event": "llm_error", **facts}]}))
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


@pytest.mark.parametrize("end_reason,error,interrupted", [
    ("client_disconnect", "opaque stream error", True),
    ("client_disconnect", "", False),
    ("producer_done", "client_disconnect", False),
    (None, "与 Agent 的连接中断，回复可能不完整", False),
])
def test_stream_interruption_requires_trace_and_failed_round(
        tmp_path, end_reason, error, interrupted):
    path = tmp_path / "trace.json"
    path.write_text(json.dumps({"end_reason": end_reason, "events": [
        {"event": "llm_error", "error_type": "AgentCancelled"}]}))
    failure = online_baseline.model_availability_failure({
        "rounds": [{"trace": str(path), "error": error}]})
    assert bool(failure) is interrupted
    if failure:
        assert failure["failure_source"] == "transport"
        assert "http_status" not in failure and "provider_error_code" not in failure


@pytest.mark.parametrize("sequence,completed,stopped", [
    (["network", "ok"], 2, False),
    (["network", "network", "network", "ok"], 3, True),
    (["network", "ok", "network", "network", "ok"], 5, False),
    (["http", "ok"], 1, True),
    (["disconnect", "ok"], 1, True),
    (["provider_401", "ok"], 1, True),
    (["provider_402", "ok"], 1, True),
    (["provider_403", "ok"], 1, True),
    (["provider_429", "ok"], 1, True),
    (["provider_503", "ok"], 1, True),
])
def test_full_collection_retains_network_errors_without_resampling(
        tmp_path, monkeypatch, sequence, completed, stopped):
    attempted = []
    cases = [{"id": str(index), "identity": "user", "tags": []}
             for index in range(len(sequence))]

    class Adapter:
        agent_url = "http://agent"
        requests = []
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
        index = int(sample["id"])
        attempted.append(index)
        kind = sequence[index]
        result = response("expected")
        facts = {"failure_source": "transport", "error_type": "ReadTimeout", "retryable": True}
        trace = {"events": []}
        if kind.startswith("provider_"):
            facts.update(failure_source="provider", http_status=int(kind.split("_")[1]))
        if kind == "disconnect":
            trace["end_reason"] = "client_disconnect"
        if kind != "ok":
            trace["events"] = [{"event": "llm_error", **facts}]
            result["error"] = "partial output interrupted"
        path = tmp_path / f"trace-{index}.json"
        path.write_text(json.dumps(trace))
        result.update(trace=str(path), round=1)
        result["rounds"] = [dict(result)]
        if kind == "http":
            result["evaluation_failure"] = {"failure_source": "http", "error_type": "ReadError"}
        return result

    monkeypatch.setattr(online_baseline.golden, "run_case", drive)
    adapter = Adapter()
    try:
        report = online_baseline.run(cases, adapter, {"user": 7}, tmp_path / "report",
            phase="full", suite_hash="synthetic", continue_transport_errors=True)
    finally:
        adapter.client.close()
    assert attempted == list(range(completed))
    assert report["full_run"] is not stopped
    assert ("stopped_reason" in report) is stopped
    assert report["automatic_retries"] == 0
    assert report["collection_policy"] == {
        "continue_transport_errors": True, "consecutive_transport_error_limit": 3}
    for index, row in enumerate(report["cases"]):
        if sequence[index] != "ok":
            assert row["status"] == "evaluation_error"
            assert row["result"]["text"] == "expected" and row["errors"]


def test_poc_cannot_opt_out_of_fail_fast_before_any_request(tmp_path):
    with pytest.raises(ValueError, match="requires a full run"):
        online_baseline.run([], None, {}, tmp_path / "report", phase="poc",
            suite_hash="synthetic", continue_transport_errors=True)
    assert not (tmp_path / "report").exists()
