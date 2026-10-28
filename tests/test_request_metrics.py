"""Accounting must preserve unknown usage and separate unlike request populations."""

import json
import math
from types import SimpleNamespace

import pytest

from eval import request_metrics, token_cost_report, usage
from utils.trace_io import load_trace


def _trace(tmp_path, name, **values):
    path = tmp_path / f"{name}.json"
    path.write_text(json.dumps(values))
    return str(path)


def test_partial_cache_coverage_uses_only_measured_denominator(tmp_path):
    events = [{"event": "llm_done", "node": "hybrid", "input": 100,
               "output": 10, "cache_read": 50, "reasoning": 0},
              {"event": "llm_done", "node": "hybrid", "input": 900, "output": 10},
              {"event": "call", "input": 100000}]
    path = _trace(tmp_path, "partial", events=events)
    row = token_cost_report.totals([path])
    assert row["calls"] == row["measured"] == 2
    assert row["in"] == 1000 and row["cache_in"] == 100
    assert row["hit_rate"] == 0.5 and row["reasoning_seen"] == 1
    assert "费用未知" in token_cost_report._row("test", row, (1, 2, 0.1))


@pytest.mark.parametrize("value", [None, True, -1, 1.5, "10"])
def test_invalid_usage_is_unmeasured(value):
    row = usage.totals([{"event": "llm_done", "input": value}])
    assert row["calls"] == 1 and row["measured"] == 0
    assert usage.hit_rate(row) is None and not usage.complete(row)


def test_impossible_cache_and_reasoning_are_not_measurements():
    row = usage.totals([{"event": "llm_done", "input": 10, "output": 3,
                         "cache_read": 11, "reasoning": 4}])
    assert row["cache_seen"] == row["reasoning_seen"] == 0
    zero = usage.totals([{"event": "llm_done", "input": 10, "output": 3,
                          "cache_read": 0, "reasoning": 0}])
    assert zero["cache_seen"] == zero["reasoning_seen"] == 1
    assert usage.hit_rate(zero) == 0


def test_request_groups_keep_source_build_model_and_path_apart(tmp_path):
    paths = []
    for index, (source, build, arm) in enumerate([
            ("production", "old", "graph"), ("production", "new", "hybrid"),
            ("evaluation", "new", "hybrid")]):
        paths.append(_trace(tmp_path, str(index), input={"source": source, "runtime": {
            "arm": arm, "build_sha": build, "model": "test-model"}}, duration_s=2,
            events=[{"event": "llm_start", "node": arm}], reply="ok"))
    groups = request_metrics.collect(paths)["groups"]
    assert len(groups) == 3
    assert all(g["started_calls"] == 1 and g["calls"] == 0 for g in groups)
    assert all(g["p95_s"] is None for g in groups)
    assert all(g["with_reply"] == 1 for g in groups)  # Presence is not task correctness.


def test_handoff_and_bad_trace_fields(tmp_path):
    doc = {"events": [{"event": "legacy_handoff", "reason": "call_budget"}],
           "input": {"source": [], "runtime": {"build_sha": {}, "model": []}},
           "duration_s": -2, "end_reason": {}}
    path = _trace(tmp_path, "handoff", **doc)
    row = request_metrics.collect([path])["groups"][0]
    assert row["path"] == "hybrid_to_graph"
    assert row["source"] == row["build"] == row["model"] == "unknown"
    assert row["duration_samples"] == 0
    assert row["handoff_reasons"] == {"call_budget": 1}
    bad = _trace(tmp_path, "bad", events=["not an event"])
    assert load_trace(bad) is None
    assert request_metrics.collect([bad])["unreadable"] == 1


def test_budget_stops_remain_visible_even_when_the_request_has_a_reply(tmp_path):
    meta = {"source": "production", "runtime": {"arm": "hybrid"}}
    paths = [_trace(tmp_path, "stopped", input=meta, reply="已停止",
                    events=[{"node": "hybrid", "event": "budget_stop", "reason": "result_budget"}]),
             _trace(tmp_path, "normal", input=meta, reply="结果", events=[])]
    group = request_metrics.collect(paths)["groups"][0]
    assert group["requests"] == group["with_reply"] == 2
    assert group["budget_stops"] == 1
    assert group["budget_stop_reasons"] == {"result_budget": 1}
    assert group["handoff_reasons"] == {}


def test_small_sample_percentiles_are_not_reported_as_p95(tmp_path):
    paths = [_trace(tmp_path, str(i), duration_s=i, events=[]) for i in range(20)]
    row = request_metrics.collect(paths)["groups"][0]
    assert row["duration_samples"] == 20 and math.isclose(row["p95_s"], 18.05)


@pytest.mark.parametrize("prices", [(1, None, 0), (1, 2, -1), (float("nan"), 2, 0)])
def test_incomplete_or_invalid_prices_are_rejected(prices):
    with pytest.raises(ValueError):
        token_cost_report._parse_price(SimpleNamespace(
            price_in=prices[0], price_out=prices[1], price_cache=prices[2]))


def test_current_runtime_label_and_archived_arm_labels_remain_readable():
    current = {"input": {"runtime": {"name": "native"}}, "events": []}
    archived = {"input": {"runtime": {"arm": "native"}}, "events": []}
    assert request_metrics.path_of(current) == request_metrics.path_of(archived) == "native"


def test_model_errors_and_measured_uncached_input_are_separate_from_missing_usage(tmp_path):
    path = _trace(tmp_path, "request", events=[
        {"event": "llm_start"}, {"event": "llm_done", "input": 100, "cache_read": 75},
        {"event": "llm_start"}, {"event": "llm_done", "input": 300},
        {"event": "llm_start"}, {"event": "llm_error"}])
    row = request_metrics.collect([path])["groups"][0]
    assert row["started_calls"] == 3 and row["calls"] == 2 and row["errored_calls"] == 1
    assert row["measured_uncached_input"] == 25 and row["in"] == 400
    assert row["cache_in"] == 100 and row["cache_seen"] == 1
    unknown = request_metrics.collect([_trace(tmp_path, "unknown", events=[])])["groups"][0]
    assert unknown["measured_uncached_input"] is None
