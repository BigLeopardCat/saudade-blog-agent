"""Incomplete or mismatched semantic evidence cannot certify frozen runs."""

import hashlib
import json

import pytest

from eval.acceptance import assess


def source(status="passed", turns=2):
    return json.dumps({"cases": [{"id": "task", "status": status,
                                 "result": {"rounds": [{}] * turns}}]}).encode()


def semantic(body, states):
    return {"source_report_sha256": hashlib.sha256(body).hexdigest(), "rows": [
        {"case": "task", "round": index + 1, "status": state}
        for index, state in enumerate(states)]}


@pytest.mark.parametrize("states,status", [
    ([], "not_evaluated"), (["no_issue_found"], "not_evaluated"),
    (["no_issue_found", "not_evaluated"], "not_evaluated"),
    (["no_issue_found", "suspect"], "needs_review"),
    (["no_issue_found", "no_issue_found"], "checks_passed"),
])
def test_all_required_rounds_need_evidence(states, status):
    body = source()
    result = assess(body, semantic(body, states))
    assert result["counts"] == {status: 1}
    assert "task_success_rate" not in result


@pytest.mark.parametrize("original,expected", [("failed", "failed"),
                                               ("evaluation_error", "not_evaluated")])
def test_judge_cannot_overturn_deterministic_failure_or_missing_evidence(original, expected):
    body = source(status=original)
    assert assess(body, semantic(body, ["no_issue_found"] * 2))["counts"] == {expected: 1}


def test_results_must_belong_to_the_frozen_source():
    body = source()
    with pytest.raises(ValueError, match="frozen source"):
        assess(body, semantic(source(status="failed"), []))


@pytest.mark.parametrize("change", ["duplicate", "unknown_case", "extra_round"])
def test_duplicate_or_unexpected_results_fail_loudly(change):
    body = source()
    report = semantic(body, ["no_issue_found"])
    if change == "duplicate":
        report["rows"] *= 2
    elif change == "unknown_case":
        report["rows"][0]["case"] = "other"
    else:
        report["rows"][0]["round"] = 3
    with pytest.raises(ValueError):
        assess(body, report)
