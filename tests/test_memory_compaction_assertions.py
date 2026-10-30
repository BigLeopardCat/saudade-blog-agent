"""Compaction acceptance needs actual replacement evidence and factual recall."""

import pytest

from eval.native_assertions import check
from eval.online_baseline import POC_IDS, load_suite


@pytest.mark.parametrize("events,passed", [
    ([], False),
    ([{"node": "memory", "event": "compaction_skipped"}], False),
    ([{"node": "memory", "event": "compacted", "source_messages": 0}], False),
    ([{"node": "memory", "event": "compacted", "source_messages": 4}], True),
])
def test_compaction_assertion_rejects_missing_failed_and_empty_replacements(events, passed):
    failures = check({"require_memory_compaction": True}, {"native_events": events})
    assert bool(failures) is not passed


def test_missing_trace_is_not_a_compaction_pass():
    assert check({"require_memory_compaction": True}, {})


@pytest.mark.parametrize("event", [
    {"node": "memory", "event": "compacted", "source_messages": 4},
    {"node": "summarizer", "event": "llm_start"},
])
def test_unnecessary_summary_calls_fail_the_history_retention_contract(event):
    assert check({"forbid_memory_compaction": True}, {"native_events": [event]})
    assert not check({"forbid_memory_compaction": True}, {"native_events": []})


def test_online_poc_recalls_original_history_without_forcing_an_early_summary():
    case = next(case for case in load_suite() if case["id"] == "memory_history_recall")
    assert case["id"] in POC_IDS
    assert sum(len(turn["user_input"]) for turn in case["rounds"][:-1]) > 9000
    assert all(turn["gold"]["forbid_memory_compaction"] for turn in case["rounds"])
    assert not any(turn["gold"].get("require_memory_compaction") for turn in case["rounds"])
    assert case["rounds"][-1]["gold"]["text_contains"] == ["松针海岸", "12"]
