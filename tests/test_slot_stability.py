"""Argument diagnostics consume current frozen HTTP evidence without model calls."""

import copy
import json

import pytest

from eval import request_metrics
from eval import slot_stability as stability


def result(*specs):
    return {"rounds": [{"confirm_payloads": [{"specs": list(specs)}]}]}


def test_all_targets_and_rounds_are_retained_with_json_types():
    row = result({"tool": "prepare", "args": {"name": "first"}},
                 {"tool": "unrelated", "args": {"name": "ignored"}},
                 {"tool": "prepare", "args": {"name": {"label": "second"}}})
    row["rounds"].append({"confirm_payloads": [{"specs": [
        {"tool": "prepare", "args": {"name": [1, True, None]}}]}]})
    assert json.loads(stability.extract_slot(row, "prepare", "name")) == [
        "first", {"label": "second"}, [1, True, None]]
    assert stability.extract_slot(row, "missing", "name") is None
    assert stability.extract_slot(row, "prepare", "missing") is None


def test_missing_values_and_failed_trials_cannot_disappear_from_denominator():
    row = stability.summarize(["a", "a", stability.MISSING, stability.MISSING])
    assert row["agreement"] == .5 and row["missing_n"] == 2 and row["trials"] == 4
    assert stability.summarize([stability.MISSING] * 4)["agreement"] == 0
    assert stability.summarize([])["agreement"] is None
    assert stability.summarize(["a"] * 4)["agreement"] == 1
    assert stability.summarize(["a", "b"])["agreement"] == .5


@pytest.fixture
def report():
    return {"suite_sha256": "suite", "selected_suite_sha256": "selected",
            "evaluator_sha256": {"online_baseline.py": "evaluator"},
            "service_version": {"sha": "build"},
            "metrics": {"unreadable": [], "groups": [
                {"source": "native", "build": "build", "model": "model"}]},
            "cases": [{"id": "task", "status": "passed", "result": result(
                {"tool": "prepare", "args": {"name": "correct"}})}]}


def write(tmp_path, name, doc):
    source = tmp_path / name
    source.write_text(json.dumps(doc))
    return source


def collect(paths):
    return stability.collect(paths, case_id="task", tool="prepare", field="name")


def test_current_reports_count_failure_with_missing_args_and_leave_evidence_intact(
        tmp_path, report):
    first = write(tmp_path, "one.json", report)
    failed = copy.deepcopy(report)
    failed["cases"][0].update(status="failed", result={})
    second = write(tmp_path, "two.json", failed)
    before = [source.read_bytes() for source in (first, second)]
    row = collect([first, second])
    assert row["trials"] == 2 and row["failed_trials"] == row["missing_n"] == 1
    assert row["agreement"] == .5 and "not task correctness" in row["interpretation"]
    assert [source.read_bytes() for source in (first, second)] == before


def test_report_identity_uses_current_request_metrics_producer(tmp_path, report):
    trace = write(tmp_path, "trace.json", {
        "input": {"source": "evaluation", "runtime": {
            "name": "langchain", "build_sha": "build", "model": "model"}},
        "events": [], "reply": "done"})
    report["metrics"] = request_metrics.collect([trace])
    assert report["metrics"]["unreadable"] == 0
    assert collect([write(tmp_path, "report.json", report)])["trials"] == 1


@pytest.mark.parametrize("metrics", [[], {}, {"groups": []}, {"groups": None}])
def test_missing_current_model_groups_are_rejected(tmp_path, report, metrics):
    report["metrics"] = metrics
    with pytest.raises(ValueError, match="measured model identity"):
        collect([write(tmp_path, "empty.json", report)])


@pytest.mark.parametrize("field", ["suite_sha256", "selected_suite_sha256",
                                   "evaluator_sha256", "service_version", "metrics"])
def test_mixed_evidence_populations_are_rejected(tmp_path, report, field):
    first = write(tmp_path, "one.json", report)
    after = copy.deepcopy(report)
    after[field] = {"changed": "value"} if isinstance(after[field], dict) else "changed"
    second = write(tmp_path, "two.json", after)
    with pytest.raises(ValueError):
        collect([first, second])


def test_duplicate_reports_unknown_status_or_missing_case_are_not_success(tmp_path, report):
    source = write(tmp_path, "one.json", report)
    with pytest.raises(ValueError, match="duplicate report"):
        collect([source, source])
    report["cases"][0]["id"] = "other"
    with pytest.raises(ValueError, match="absent"):
        collect([write(tmp_path, "other.json", report)])
    report["cases"][0].update(id="task", status="skipped")
    with pytest.raises(ValueError, match="status"):
        collect([write(tmp_path, "skipped.json", report)])


def test_single_report_must_match_measured_and_advertised_builds(tmp_path, report):
    report["metrics"]["groups"][0]["build"] = "other-build"
    with pytest.raises(ValueError, match="measured build differs"):
        collect([write(tmp_path, "contradictory.json", report)])


@pytest.mark.parametrize("fault", ["missing_service", "missing_sha", "numeric_sha",
                                   "missing_metric_build", "numeric_metric_build",
                                   "invalid_metric_row"])
def test_incomplete_or_untyped_build_evidence_is_not_usable(tmp_path, report, fault):
    if fault == "missing_service":
        report.pop("service_version")
    elif fault == "missing_sha":
        report["service_version"].pop("sha")
    elif fault == "numeric_sha":
        report["service_version"]["sha"] = 7
    elif fault == "missing_metric_build":
        report["metrics"]["groups"][0].pop("build")
    elif fault == "numeric_metric_build":
        report["metrics"]["groups"][0]["build"] = 7
    else:
        report["metrics"]["groups"] = [None]
    with pytest.raises(ValueError):
        collect([write(tmp_path, fault + ".json", report)])


def test_archived_runtime_label_and_typed_slot_values_remain_usable(tmp_path, report):
    report["metrics"]["groups"][0]["source"] = "native"
    report["cases"][0]["result"] = result({"tool": "prepare", "args": {"name": [7, True, None]}})
    before = write(tmp_path, "archive.json", report)
    row = collect([before])
    assert row["agreement"] == 1 and row["missing_n"] == row["failed_trials"] == 0
    assert json.loads(row["draws"][0]["value"]) == [[7, True, None]]
