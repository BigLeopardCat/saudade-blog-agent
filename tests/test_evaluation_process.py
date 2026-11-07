"""Scenario efficiency checks distinguish model attempts from parallel tool execution."""

import json

import pytest

from eval import native_assertions, online_baseline


@pytest.mark.parametrize("attempts,limit,passed", [(2, 2, True), (4, 2, False), (0, 0, True)])
def test_call_acceptance_counts_model_attempts_including_summary_and_retry(attempts, limit, passed):
    events = [{"node": "native" if index % 2 else "summarizer", "event": "llm_start"}
              for index in range(attempts)]
    events += [{"node": "native", "event": "tool_requested", "name": "search_public_activity"}
               for _ in range(4)]
    failures = native_assertions.check({"max_model_calls": limit}, {"native_events": events})
    assert (not failures) is passed


@pytest.mark.parametrize("maximum", [True, -1, "2", None])
def test_invalid_scenario_call_limit_fails_before_an_online_run(tmp_path, maximum):
    suite = tmp_path / "suite.jsonl"
    suite.write_text(json.dumps({"id": "bounded-search", "identity": "user",
        "user_input": "search", "gold": {"max_model_calls": maximum}}) + "\n")
    with pytest.raises(ValueError, match="model-call acceptance limit"):
        online_baseline.load_suite(suite)


def test_call_budget_requires_trace_evidence():
    assert "未评估" in native_assertions.check({"max_model_calls": 2}, {})[0]
