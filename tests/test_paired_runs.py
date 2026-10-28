"""A comparison must keep the same cases, judgments and immutable request evidence."""

import copy
import hashlib
import json

import pytest

from eval.compare_runs import compare


def report(tmp_path):
    trace = tmp_path / "trace.json"
    trace.write_text(json.dumps({"events": [
        {"event": "llm_done", "input": 100, "output": 10, "cache_read": 50}]}))
    round_ = {"trace": str(trace), "http_timing": {
        "trace_sha256": hashlib.sha256(trace.read_bytes()).hexdigest()}}
    cases = [{"id": str(i), "case_sha256": f"case-{i}", "status": "passed",
              "result": {"rounds": [round_, round_]}} for i in range(3)]
    return {"cases": cases, "full_run": True, "suite_sha256": "suite"}, trace


def test_paired_cost_includes_all_turns_but_resamples_complete_cases(tmp_path):
    source, _ = report(tmp_path)
    candidate = copy.deepcopy(source)
    candidate["cases"][0]["status"] = "failed"
    got = compare(source, candidate)
    assert got["quality"]["pairs"] == 3 and got["quality"]["losses"] == 1
    assert got["regressions"] == ["0"]
    assert got["cost"]["input"]["pairs"] == 3
    assert got["cost"]["input"]["paired_bootstrap_95"] == [0, 0]


@pytest.mark.parametrize("fault", ["suite", "assertions", "incomplete", "error", "trace"])
def test_invalid_comparisons_do_not_emit_an_improvement(tmp_path, fault):
    source, trace = report(tmp_path)
    candidate = copy.deepcopy(source)
    if fault == "suite":
        candidate["suite_sha256"] = "different"
    elif fault == "assertions":
        candidate["cases"][0]["case_sha256"] = "different"
    elif fault == "incomplete":
        candidate["full_run"] = False
    elif fault == "error":
        candidate["cases"][0]["status"] = "evaluation_error"
    else:
        trace.write_bytes(trace.read_bytes() + b"\n")
    with pytest.raises(ValueError):
        compare(source, candidate)
