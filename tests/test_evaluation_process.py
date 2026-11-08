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
    with pytest.raises(ValueError, match="max_model_calls acceptance limit"):
        online_baseline.load_suite(suite)


def test_call_budget_requires_trace_evidence():
    assert "未评估" in native_assertions.check({"max_model_calls": 2}, {})[0]


@pytest.mark.parametrize("queries,passed", [
    (["猫咪"], True), (["猫咪", "小猫", "喵"], True), (["小猫"], False),
    ("猫咪", False), ([], False), (None, False),
])
def test_query_expansion_keeps_the_requested_term(queries, passed):
    wanted = {"tools": ["search_public_activity"], "kinds": ["ok"],
              "args_list_contains": {"keywords": ["猫咪"]}}
    result = {"native_events": [{"node": "native", "event": "call",
        "name": "search_public_activity", "kind": "ok", "args": {"keywords": queries}}]}
    failures = native_assertions.check({"require_observations": [wanted]}, result)
    assert (not failures) is passed


@pytest.mark.parametrize("requests,passed", [(1, True), (4, False)])
def test_one_model_batch_cannot_hide_repeated_search_requests(requests, passed):
    events = [{"node": "native", "event": "llm_start"}]
    events += [{"node": "native", "event": "tool_requested", "name": "search_public_activity"}
               for _ in range(requests)]
    gold = {"max_model_calls": 2, "max_tool_calls": {"search_public_activity": 1}}
    assert (not native_assertions.check(gold, {"native_events": events})) is passed


@pytest.mark.parametrize("limits", [None, [], {"search_public_activity": True},
                                    {"search_public_activity": -1}, {"": 1}])
def test_invalid_tool_limits_fail_before_online_spending(tmp_path, limits):
    suite = tmp_path / "suite.jsonl"
    suite.write_text(json.dumps({"id": "batch", "identity": "user", "user_input": "search",
                                 "gold": {"max_tool_calls": limits}}) + "\n")
    with pytest.raises(ValueError, match="tool-call acceptance limit"):
        online_baseline.load_suite(suite)


def test_exact_argument_lists_still_require_length_and_order():
    assert not native_assertions.contains(["猫咪", "小猫"], ["猫咪"])
    assert not native_assertions.contains(["小猫", "猫咪"], ["猫咪", "小猫"])


def test_tool_limits_require_trace_and_suite_accepts_batched_followup():
    assert "未评估" in native_assertions.check({"max_tool_calls": {"search": 1}}, {})[0]
    case = next(row for row in online_baseline.load_suite()
                if row["id"] == "activity_keyword_followup")
    assert all(rnd["gold"]["max_tool_calls"] == {"search_public_activity": 1}
               for rnd in case["rounds"])
