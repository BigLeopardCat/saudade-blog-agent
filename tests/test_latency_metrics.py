"""Latency attribution cannot turn missing data into free time or queue measurements."""

import math

import pytest

from eval import latency_metrics


def event(kind, *, node="native", **values):
    return {"event": kind, "node": node, **values}


def test_model_phases_keep_summary_and_approval_separate_with_coverage():
    calls = latency_metrics.model_calls([
        event("llm_start", round=0, phase="execution"),
        event("model_first_token", round=0, duration_s=2),
        event("model_first_token", round=0, duration_s=3),
        event("llm_done", round=0, duration_s=5),
        event("llm_start", round=1, phase="approval_response"),
        event("llm_error", round=1, duration_s=7),
        event("llm_start", node="summarizer"),
        event("llm_done", node="summarizer", duration_s=4),
        event("llm_start", round=2, phase="execution"),
    ])
    assert [call["phase"] for call in calls] == [
        "execution", "approval_response", "summary", "execution"]
    assert calls[0]["first_token_s"] == 2 and calls[0]["remaining_s"] == 3
    assert calls[1]["first_token_s"] is None and calls[2]["first_token_s"] is None
    groups = latency_metrics.aggregate(calls)
    assert groups["execution"]["attempts"] == 2 and groups["execution"]["unfinished"] == 1
    assert groups["execution"]["total_s"]["samples"] == 1
    assert groups["approval_response"]["errors"] == 1
    assert groups["summary"]["total_s"]["p50_s"] == 4
    assert all(row["total_s"]["p95_s"] is None for row in groups.values())


@pytest.mark.parametrize("value", [True, -1, "3", math.nan, math.inf, None])
def test_invalid_duration_is_not_zero_or_a_latency_sample(value):
    calls = latency_metrics.model_calls([event("llm_start"), event("llm_done", duration_s=value)])
    assert calls[0]["total_s"] is None
    assert latency_metrics.aggregate(calls)["unknown"]["total_s"]["samples"] == 0


def test_tools_match_by_call_identity_and_parallel_time_is_not_summed():
    calls = latency_metrics.tool_calls([
        event("tool_requested", call_id="a", name="first", t=1),
        event("tool_requested", call_id="b", name="second", t=1.5),
        event("call", call_id="b", t=2), event("call", call_id="a", t=3),
        event("tool_requested", call_id="c", name="loader", t=4),
        event("load_domain", call_id="c", t=4.1),
        event("tool_requested", call_id="d", name="lost", t=5),
        event("tool_error", name="lost", t=6),
    ])
    assert calls[0]["elapsed_s"] == 2 and calls[1]["elapsed_s"] == 0.5
    assert calls[2]["kind"] == "discovery"
    assert calls[3]["status"] == "unfinished" and calls[3]["elapsed_s"] is None


def test_late_tokens_and_unmatched_events_do_not_attach_to_a_finished_call():
    calls = latency_metrics.model_calls([
        event("llm_done", round=9, duration_s=100),
        event("llm_start", round=0), event("llm_done", round=0, duration_s=1),
        event("model_first_token", round=0, duration_s=0.5),
        event("llm_start", round=1), event("model_first_token", round=1, duration_s=3),
        event("llm_done", round=1, duration_s=1),
    ])
    assert len(calls) == 2 and calls[0]["first_token_s"] is None
    assert calls[1]["remaining_s"] is None
