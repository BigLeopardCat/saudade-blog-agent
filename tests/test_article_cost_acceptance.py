"""Explicit reading efficiency diagnostics cannot suppress quality or evidence checks."""

import json

import pytest

from eval.golden_evaluation import check_gold
from eval.online_baseline import load_suite


def evidence(*sizes):
    return {"node": "native", "event": "article_evidence", "provenance": {"body_reads": [
        {"id": 19, "ranges": [{"start": 0, "end": n, "visible_chars": n} for n in sizes]}]}}


@pytest.mark.parametrize("events,valid", [
    ([evidence(842)], True),
    ([evidence(12000, 13821)], False),
    ([evidence(3000), evidence(3000)], True),
    ([evidence(1000), evidence(5000)], False),
    ([], False),
    ([{"event": "article_evidence", "provenance": {}}], False),
    ([evidence(True)], False),
    ([evidence(-1)], False),
])
def test_actual_ranges_reject_overreading_even_with_only_three_model_calls(events, valid):
    starts = [{"node": "native", "event": "llm_start"}] * 3
    result = {"text": "已查到文档记载。", "tool_calls": [], "commands": [], "exec_rows": [],
              "native_events": [*starts, *events]}
    assert (not check_gold({"max_model_calls": 3, "max_article_body_chars": 4000}, result)) is valid


@pytest.mark.parametrize("limit", [True, -1, 4.5])
def test_malformed_body_limits_are_rejected_before_requests(tmp_path, limit):
    case = {"id": "body-budget", "identity": "admin", "tags": ["read"],
            "user_input": "查文档配置", "gold": {"max_article_body_chars": limit}}
    path = tmp_path / "cases.jsonl"
    path.write_text(json.dumps(case) + "\n")
    with pytest.raises(ValueError, match="acceptance limit"):
        load_suite(path)


def test_opted_in_efficiency_targets_preserve_measurements_without_failing_quality():
    gold = {"max_model_calls": 3, "max_article_body_chars": 4000,
            "diagnostic_limits": ["max_model_calls", "max_article_body_chars"]}
    result = {"text": "已查到文档记载。", "tool_calls": [], "commands": [], "exec_rows": [],
              "native_events": [{"event": "llm_start"}] * 4 + [evidence(7178)]}
    assert not check_gold(gold, result)
    assert result["efficiency_findings"] == [
        {"metric": "max_model_calls", "observed": 4, "target": 3},
        {"metric": "max_article_body_chars", "observed": 7178, "target": 4000}]
    assert not check_gold(gold, result)
    assert len(result["efficiency_findings"]) == 2
    result["text"] = ""
    assert check_gold(gold, result)
    result["text"] = "已查到文档记载。"
    assert check_gold({**gold, "require_body_reads": [{"article_id": 19}]}, result)
    result["native_events"] = []
    assert check_gold(gold, result)


@pytest.mark.parametrize("diagnostic", [
    None, "max_model_calls", ["max_tool_calls"], ["nonempty"],
    ["max_model_calls", "max_model_calls"], ["max_article_body_chars"],
])
def test_invalid_diagnostic_contract_is_rejected_before_requests(tmp_path, diagnostic):
    case = {"id": "diagnostic", "identity": "user", "tags": ["read"],
            "user_input": "查文档配置", "gold": {
                "max_model_calls": 3, "diagnostic_limits": diagnostic}}
    path = tmp_path / "cases.jsonl"
    path.write_text(json.dumps(case) + "\n")
    with pytest.raises(ValueError, match="diagnostic limits"):
        load_suite(path)
