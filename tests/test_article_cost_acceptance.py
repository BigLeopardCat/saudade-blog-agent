"""Focused reads need a volume ceiling as well as a model-call ceiling."""

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
