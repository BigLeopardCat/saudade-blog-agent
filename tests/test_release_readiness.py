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
            {"node": "native", "event": "llm_done", "tool_calls": [],
             "input": 100, "output": 10, "cache_read": 0},
        ]}))
    cases = [{"id": str(index), "tags": ["chat", "identity"], "status": "passed",
              "first_action": {"any_tools": [], "allow_text": True},
              "result": {"rounds": [
        {"trace": str(trace), "http_timing": {"elapsed_s": 2, "first_visible_s": 0.2,
         "first_reply_s": 1,
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
    assert got["quality_gate_statistics"]["task"]["combined_checks"] is None
    assert got["quality_gate_statistics"]["task"]["wilson_lower_meets_target"] is None


def test_wilson_diagnostic_does_not_replace_the_observed_rate_gate(tmp_path):
    report, profile, semantic, _ = fixture(tmp_path)
    got = assess(report, semantic_path=semantic, profile_path=profile)
    assert got["status"] == "http_checks_passed"
    for name in ("task", "core"):
        row = got["quality_gate_statistics"][name]
        assert row["combined_checks"]["total"] == 20
        assert row["combined_checks"]["rate"] == 1
        assert row["combined_checks"]["wilson_95"][0] < 0.84
        assert row["wilson_lower_meets_target"] is False
        assert row["wilson_is_release_gate"] is False


def test_core_statistics_keep_unresolved_cases_and_do_not_count_rounds():
    from eval.release_readiness import quality_gate_statistics

    cases = [{"id": "read_ok", "tags": ["read"], "result": {"rounds": [{}, {}]}},
             {"id": "core_missing", "tags": ["read"]},
             {"id": "chat", "tags": ["chat"]}]
    got = quality_gate_statistics(cases, {"read_ok": "checks_passed", "chat": "checks_passed"},
                                  json.loads(PROFILE.read_bytes()))
    assert got["task"]["combined_checks"]["rate"] == 2 / 3
    assert got["core"]["combined_checks"]["total"] == 2
    assert got["core"]["combined_checks"]["rate"] == 0.5
    assert got["core"]["unresolved_semantic"] == 1
    assert got["core"]["wilson_lower_meets_target"] is None
    empty = quality_gate_statistics([], {}, json.loads(PROFILE.read_bytes()))
    assert empty["core"]["combined_checks"] is None


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


def test_outcome_costs_keep_failed_spending_and_unreviewed_success_separate():
    from eval import usage
    from eval.release_readiness import outcome_usage

    cases = [{"id": "ok", "status": "passed"}, {"id": "failed", "status": "failed"},
             {"id": "unknown", "status": "passed"}]
    measured = {case["id"]: usage.totals([{"event": "llm_done", "input": 100,
                                         "output": 10, "cache_read": 50}]) for case in cases}
    got = outcome_usage(cases, {"ok": "checks_passed"}, measured)
    assert set(got) == {"passed", "failed", "unverified"}
    assert all(row["scenarios"] == 1 and row["in"] == 100 and row["uncached_input"] == 50
               and row["fee"] is None for row in got.values())


def test_absent_cache_usage_stays_unknown_in_cache_diagnostics(tmp_path):
    report, profile, _, trace = fixture(tmp_path)
    doc = json.loads(trace.read_bytes())
    doc["events"][-1].pop("cache_read")
    trace.write_text(json.dumps(doc))
    got = assess(report, profile_path=profile)
    assert got["cache_diagnostics"]["first_call"]["uncached_input"] is None


def test_model_error_cost_cannot_disappear_behind_complete_successful_calls(tmp_path):
    report, profile, _, trace = fixture(tmp_path)
    doc = json.loads(trace.read_bytes())
    doc["events"].append({"event": "llm_error"})
    trace.write_text(json.dumps(doc))
    got = assess(report, profile_path=profile)
    assert any("errored or unfinished calls" in item for item in got["unverified"])


def test_task_classes_handle_overlapping_suite_tags():
    assert task_class(["write_prepare", "multi_intent"]) == "multi"
    assert task_class(["confirmation", "memory"]) == "control"
    assert task_class(["retrieval", "dependency"]) == "article_read"
    assert task_class(["accounts", "moderation", "negative"]) == "read"
    assert task_class(["accounts", "write_prepare"]) == "prepare"
    assert task_class(["accounts", "memory"]) == "conversation"
    assert task_class(["security", "negative"]) == "chat"


def test_noncritical_semantic_question_stays_unverified_within_quality_budgets(tmp_path):
    report, profile, semantic, _ = fixture(tmp_path, count=100)
    write_review(report, semantic, suspect={"99"})
    got = assess(report, semantic_path=semantic, profile_path=profile)
    assert got["status"] == "unverified" and not got["failures"]
    assert any("require review: 99" in item for item in got["unverified"])
    quality = got["quality_statistics"]["all"]
    assert quality["combined_checks"]["passed"] == 99
    assert quality["combined_checks"]["total"] == 100
    assert quality["unresolved_semantic"] == 1


def test_known_noncritical_failure_can_use_quality_budget_without_becoming_unknown(tmp_path):
    report, profile, semantic, _ = fixture(tmp_path, count=100)
    doc = json.loads(report.read_bytes())
    doc["cases"][-1]["status"] = "failed"
    report.write_text(json.dumps(doc))
    write_review(report, semantic)
    got = assess(report, semantic_path=semantic, profile_path=profile)
    assert got["status"] == "http_checks_passed" and not got["unverified"]
    quality = got["quality_statistics"]["all"]
    assert quality["combined_checks"]["passed"] == 99
    assert quality["combined_checks"]["total"] == 100
    assert quality["unresolved_semantic"] == 0


def test_dispatch_rejections_are_reported_and_cannot_earn_first_action_credit(tmp_path):
    report, profile, semantic, trace = fixture(tmp_path, count=1)
    doc = json.loads(trace.read_bytes())
    doc["events"][-1]["tool_calls"] = ["list_notes"]
    doc["events"].append({"node": "native", "event": "tool_error",
                          "name": "list_notes", "reason": "tool_not_loaded"})
    trace.write_text(json.dumps(doc))
    source = json.loads(report.read_bytes())
    case = source["cases"][0]
    case["first_action"] = {"any_tools": ["list_notes"], "allow_text": False}
    case["result"]["rounds"][0]["http_timing"]["trace_sha256"] = (
        hashlib.sha256(trace.read_bytes()).hexdigest())
    report.write_text(json.dumps(source))
    write_review(report, semantic)
    got = assess(report, semantic_path=semantic, profile_path=profile)
    assert got["tool_process"]["totals"]["inactive_tool_requests"] == 1
    assert got["tool_process"]["rejected_rounds"][0]["case"] == "0"
    assert any(row["gate"] == "first_action" for row in got["failures"])


def test_cache_population_uses_wire_identity_and_preserves_unobserved_usage():
    sdk = {"event": "request_layout", "model": "m", "tools": "t", "messages": ["a"],
           "session": "stable"}
    wire = dict(sdk, event="wire_request")
    done = {"event": "llm_done", "input": 100, "cache_read": 0, "output": 10}
    doc = {"events": [sdk, wire, done, sdk, dict(wire, session="changed"), done,
                      sdk, {"event": "wire_request_unavailable"}, done, sdk, done]}
    rows = list(cache_population(doc))
    assert [label for label, _ in rows] == [
        "first_call", "changed_layout", "unknown_layout", "first_call"]
    assert sum(row["input"] for _, row in rows) == 400
    assert all("known_prefix_input" not in row for _, row in rows)
