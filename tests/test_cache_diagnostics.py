"""Payload changes and provider cache observations are independent evidence."""

import json

from eval.cache_diagnostics import compare, diagnose


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
