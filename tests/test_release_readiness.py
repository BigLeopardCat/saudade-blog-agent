"""Release evidence must not turn missing measurements or unsafe outcomes green."""

import hashlib
import json

import pytest

from eval.release_readiness import PROFILE, assess, cache_population, distribution, task_class


def fixture(tmp_path, count=20):
    trace = tmp_path / "trace.json"
    trace.write_text(json.dumps({
        "input": {"runtime": {"name": "native", "build_sha": "build", "model": "model"}},
        "reply": "hello", "events": [
            {"event": "request_layout", "model": "model", "tools": "tools", "messages": ["a"]},
            {"event": "llm_done", "input": 100, "output": 10, "cache_read": 0},
        ]}))
    cases = [{"id": str(index), "tags": ["chat", "identity"], "status": "passed",
              "result": {"rounds": [
        {"trace": str(trace), "http_timing": {"elapsed_s": 2, "first_visible_s": 0.2,
         "trace_sha256": hashlib.sha256(trace.read_bytes()).hexdigest()}}
    ]}} for index in range(count)]
    report = tmp_path / "report.json"
    report.write_text(json.dumps({"cases": cases, "full_run": True, "planned": count,
                                  "service_version": {"sha": "build"}}))
    profile = json.loads(PROFILE.read_bytes())
    profile.update(min_full_cases=1, critical_cases=["0"], task_budgets={
        "chat": profile["task_budgets"]["chat"]})
    profile_path = tmp_path / "profile.json"
    profile_path.write_text(json.dumps(profile))
    doc = json.loads(report.read_bytes())
    doc["evaluator_sha256"] = {"release_profile.json":
                               hashlib.sha256(profile_path.read_bytes()).hexdigest()}
    report.write_text(json.dumps(doc))
    semantic_path = tmp_path / "semantic.json"
    write_review(report, semantic_path)
    return report, profile_path, semantic_path, trace


def write_review(report, target, suspect=()):
    cases = json.loads(report.read_bytes())["cases"]
    digest = hashlib.sha256(report.read_bytes()).hexdigest()
    target.write_text(json.dumps({"source_report_sha256": digest,
        "rows": [{"case": case["id"], "round": 1,
                  "status": "suspect" if case["id"] in suspect else "no_issue_found"}
                 for case in cases]}))


def test_complete_http_evidence_passes_without_claiming_browser_or_write_execution(tmp_path):
    report, profile, semantic, _ = fixture(tmp_path)
    got = assess(report, semantic_path=semantic, profile_path=profile)
    assert got["status"] == "http_checks_passed"
    assert got["usage"]["in"] == 2000 and got["usage"]["cache"] == 0
    assert got["cache_diagnostics"]["first_call"]["uncached_input"] == 2000
    assert got["separate_evidence_required"]


def test_missing_semantic_review_is_unverified(tmp_path):
    report, profile, _, _ = fixture(tmp_path)
    got = assess(report, profile_path=profile)
    assert got["status"] == "unverified" and not got["semantic_counts"]


def test_one_good_sample_cannot_establish_p95(tmp_path):
    report, profile, semantic, _ = fixture(tmp_path, count=1)
    got = assess(report, semantic_path=semantic, profile_path=profile)
    assert got["status"] == "unverified"
    assert got["task_metrics"]["chat"]["in"]["p95"] is None
    assert distribution([1], 20) == {"samples": 1, "p50": 1, "p95": None, "max": 1}


def test_a_critical_semantic_failure_blocks_even_when_overall_pass_rate_is_95_percent(tmp_path):
    report, profile, semantic, _ = fixture(tmp_path)
    write_review(report, semantic, suspect={"0"})
    got = assess(report, semantic_path=semantic, profile_path=profile)
    assert got["status"] == "failed"
    assert "semantic_critical" in {f["gate"] for f in got["failures"]}


@pytest.mark.parametrize("fault", ["missing_trace", "wrong_build", "missing_usage",
                                 "business_write"])
def test_evidence_faults_never_pass(tmp_path, fault):
    report, profile, semantic, trace = fixture(tmp_path)
    data = json.loads(trace.read_bytes())
    if fault == "missing_trace":
        trace.unlink()
    elif fault == "wrong_build":
        data["input"]["runtime"]["build_sha"] = "other"
        trace.write_text(json.dumps(data))
    elif fault == "missing_usage":
        data["events"][-1].pop("cache_read")
        trace.write_text(json.dumps(data))
    else:
        doc = json.loads(report.read_bytes())
        doc["cases"][0]["result"]["rounds"][0]["exec_rows"] = [{"tool": "set_role"}]
        report.write_text(json.dumps(doc))
        write_review(report, semantic)
    result = assess(report, semantic_path=semantic, profile_path=profile)
    assert result["status"] != "http_checks_passed"


def test_efficiency_regression_blocks_despite_good_semantic_results(tmp_path):
    report, profile, semantic, trace = fixture(tmp_path)
    doc = json.loads(trace.read_bytes())
    doc["events"][-1]["input"] = 9000
    trace.write_text(json.dumps(doc))
    got = assess(report, semantic_path=semantic, profile_path=profile)
    assert any(f["gate"] == "chat.in" for f in got["failures"])


def test_missing_critical_cases_and_incomplete_runs_are_unverified(tmp_path):
    report, profile, semantic, _ = fixture(tmp_path)
    doc = json.loads(profile.read_bytes())
    doc["critical_cases"].append("omitted_security_case")
    profile.write_text(json.dumps(doc))
    got = assess(report, semantic_path=semantic, profile_path=profile)
    assert got["status"] == "unverified"
    assert any("omitted_security_case" in row for row in got["unverified"])


def test_modified_trace_is_rejected_after_the_baseline_was_frozen(tmp_path):
    report, profile, semantic, trace = fixture(tmp_path)
    trace.write_bytes(trace.read_bytes() + b"\n")
    got = assess(report, semantic_path=semantic, profile_path=profile)
    assert got["status"] == "failed"
    assert got["failures"][0]["detail"][0]["error"] == "trace changed"


def test_semantic_results_must_match_the_source_digest(tmp_path):
    report, profile, semantic, _ = fixture(tmp_path)
    report.write_bytes(report.read_bytes() + b"\n")
    with pytest.raises(ValueError, match="frozen source"):
        assess(report, semantic_path=semantic, profile_path=profile)


def test_cache_ratio_separates_layout_changes_and_exact_appends():
    def layout(tools, messages):
        return {"event": "request_layout", "model": "m", "tools": tools, "messages": messages}
    done = {"event": "llm_done", "input": 100, "cache_read": 50, "output": 10}
    doc = {"events": [layout("a", ["1"]), done,
                      layout("a", ["1", "2"]), done,
                      layout("b", ["1", "2", "3"]), done,
                      layout("b", ["1", "3", "2"]), done, done]}
    assert [label for label, _ in cache_population(doc)] == [
        "first_call", "stable_prefix", "changed_layout", "changed_layout", "unknown_layout"]


def test_new_tool_output_is_not_mistaken_for_a_missed_old_prefix():
    doc = {"events": [
        {"event": "request_layout", "model": "m", "tools": "t", "messages": ["a"]},
        {"event": "llm_done", "input": 100, "output": 10, "cache_read": 0},
        {"event": "request_layout", "model": "m", "tools": "t", "messages": ["a", "b"]},
        {"event": "llm_done", "input": 300, "output": 10, "cache_read": 100},
    ]}
    label, measured = list(cache_population(doc))[1]
    assert label == "stable_prefix"
    assert measured["known_prefix_input"] == measured["known_prefix_reused"] == 100
    assert measured["cache_read"] / measured["input"] == 1 / 3


def test_task_classes_handle_overlapping_suite_tags():
    assert task_class(["write_prepare", "multi_intent"]) == "multi"
    assert task_class(["confirmation", "memory"]) == "control"
    assert task_class(["retrieval", "dependency"]) == "article_read"
