"""Payload changes and provider cache observations are independent evidence."""

import json

from eval.cache_diagnostics import compare, diagnose, request_cohorts


def document(messages, *, cache=0, tools=None, flags=(), timestamp="2026-11-01T12:00:00+08:00"):
    return {"started_at": timestamp, "events": [
        *({"event": flag} for flag in flags),
        {"event": "request_layout", "model": "m", "endpoint": "endpoint-hash",
         "session": "session-hash", "messages": messages, "tools": str(tools),
         "tool_fingerprints": tools or [], "controls": "control-hash"},
        {"event": "llm_done", "node": "native", "input": 100, "output": 5,
         **({"cache_read": cache} if cache is not None else {})}]}


def test_identical_payload_with_reported_zero_is_not_called_an_application_cache_hit():
    got = diagnose([("a", document(["a"])), ("b", document(["a"]))])
    assert got["calls"][1]["change"] == "same_payload"
    assert got["calls"][1]["cache_observation"] == "reported_miss"
    assert got["calls"][0]["change"] == "first_observed"


def test_tools_appended_and_messages_changed_have_independent_first_positions():
    documents = [("a", document(["a"], tools=["t"])),
                 ("b", document(["a", "b"], tools=["t", "u"], cache=50)),
                 ("c", document(["a", "c"], tools=["t", "u"]))]
    rows = diagnose(documents)["calls"]
    assert rows[1]["change"] == "tool_schema_changed" and rows[1]["tools_appended"]
    assert rows[1]["first_changed_tool"] == rows[1]["first_changed_message"] == 1
    assert rows[2]["change"] == "message_prefix_changed"
    assert rows[2]["first_changed_message"] == 1


def test_compaction_and_resume_are_flags_not_an_invented_cache_cause():
    got = diagnose([("a", document(["a"])), ("b", document(
        ["summary"], cache=None, flags=("task_resumed", "compacted")))])
    row = got["calls"][1]
    assert row["flags"] == ["compacted", "task_resumed"]
    assert row["cache_observation"] == "unknown" and row["uncached_input"] is None
    assert "session-hash" not in json.dumps(got)


def test_missing_aware_time_or_endpoint_cannot_compare_different_requests():
    documents = [("a", document(["a"], timestamp="2026-11-01T12:00:00")),
                 ("b", document(["a"], timestamp="2026-11-01T12:01:00"))]
    assert all(row["change"] == "first_observed" for row in diagnose(documents)["calls"])
    assert compare({"messages": ["a"], "controls": "a"},
                   {"messages": ["a"], "controls": "b"})["change"] == "controls_changed"


def test_summary_calls_are_not_compared_with_main_agent_calls():
    summary = document(["other"])
    summary["events"][-1]["node"] = "summarizer"
    got = diagnose([("a", document(["a"])), ("b", summary),
                    ("c", document(["a", "b"], cache=50))])
    assert [row["change"] for row in got["calls"]] == [
        "first_observed", "first_observed", "append_only"]


def test_changed_generation_parameter_is_identified_without_exposing_its_value():
    before = {"control_fingerprints": {"max_tokens": "a"}, "controls": "a"}
    after = {"control_fingerprints": {"max_tokens": "b", "seed": "c"}, "controls": "b"}
    assert compare(before, after)["changed_control_keys"] == ["max_tokens", "seed"]


def test_more_complete_thinking_summary_does_not_change_the_frozen_control_hash():
    before = {"messages": ["a"], "controls": "same", "thinking": None}
    after = {**before, "thinking": {"type": "disabled"}}
    assert compare(before, after)["change"] == "same_payload"
    after["controls"] = "different"
    assert compare(before, after)["change"] == "controls_changed"


def test_request_cohorts_count_whole_requests_and_include_summary_cost():
    first = document(["a"], cache=60, timestamp="2026-11-01T12:00:00+08:00")
    continuing = document(["a", "b"], cache=90, timestamp="2026-11-01T12:01:00+08:00")
    compact = document(["summary"], cache=20, flags=("compacted",),
                       timestamp="2026-11-01T12:02:00+08:00")
    compact["events"].insert(0, {"event": "llm_done", "node": "summarizer",
                                 "input": 200, "output": 10, "cache_read": 0})
    following = document(["summary", "c"], cache=80, timestamp="2026-11-01T12:03:00+08:00")
    later = document(["summary", "c", "d"], cache=80,
                     timestamp="2026-11-01T12:04:00+08:00")
    rows = request_cohorts([("e", later), ("c", compact), ("a", first),
                           ("d", following), ("b", continuing)], (0.30, 1.20, 0.006))
    groups = {row["cohort"]: row for row in rows}
    assert groups["first_observed_request"]["requests"] == 1
    assert groups["continued_request"]["requests"] == 2
    assert groups["first_request_after_compaction"]["requests"] == 1
    row = groups["compaction_request"]
    assert row["requests"] == 1 and row["calls"] == 2
    assert row["in"] == 300 and row["out"] == 15 and row["cache"] == 20
    assert abs(row["estimated_cost"] - (280*.30 + 20*.006 + 15*1.20)/1e6) < 1e-12
    assert "session-hash" not in json.dumps(rows)


def test_unknown_cache_failed_attempt_or_mixed_model_never_has_a_known_fee():
    for variant in ("cache", "failure", "mixed"):
        doc = document(["a"], cache=None if variant == "cache" else 90)
        if variant == "failure":
            doc["events"].append({"event": "llm_error", "node": "native"})
        if variant == "mixed":
            doc["events"].append({"event": "request_layout", "model": "other"})
        row = request_cohorts([("a", doc)], (0.30, 1.20, 0.006))[0]
        assert row["estimated_cost"] is None


def test_missing_identity_stays_unclassified_and_prices_are_optional():
    doc = document(["a"], timestamp="2026-11-01T12:00:00")
    rows = request_cohorts([("a", doc), ("b", doc)])
    assert len(rows) == 1 and rows[0]["requests"] == 2
    assert rows[0]["cohort"] == "unclassified" and rows[0]["estimated_cost"] is None


def test_failed_summary_is_a_compaction_cost_but_does_not_mark_the_next_request_compacted():
    first = document(["a"], cache=60)
    first["events"].extend([{"event": "llm_start", "node": "summarizer"},
                             {"event": "llm_error", "node": "summarizer"}])
    following = document(["a", "b"], cache=90, timestamp="2026-11-01T12:01:00+08:00")
    rows = request_cohorts([("a", first), ("b", following)], (0.3, 1.2, 0.006))
    groups = {row["cohort"]: row for row in rows}
    assert set(groups) == {"compaction_request", "continued_request"}
    assert groups["compaction_request"]["unknown_attempts"] == 1
    assert groups["compaction_request"]["estimated_cost"] is None


def test_estimated_cost_includes_reasoning_in_output_only_once():
    doc = document(["a"], cache=60)
    doc["events"][-1]["reasoning"] = 5
    row = request_cohorts([("a", doc)], (0.3, 1.2, 0.006))[0]
    assert abs(row["estimated_cost"] - (40*.3 + 60*.006 + 5*1.2)/1e6) < 1e-12
