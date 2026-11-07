"""Legacy event interpretation belongs to the read-only archive diagnostic."""

from copy import deepcopy

import pytest

from eval import trace_metrics


@pytest.mark.parametrize(("fields", "expected"), [
    ({"skill": "chat"}, "chat"),
    ({"workflow": "navigate"}, "navigate"),
    ({"skill": "old", "workflow": "new"}, "new"),
])
def test_archived_workflow_names_are_read_without_modifying_evidence(fields, expected):
    document = {"reply": "archive", "events": [
        {"node": "planner", "event": "decision", "tools": ["read_article({})"], **fields},
        {"node": "gate", "event": "fallback"},
        {"node": "execute", "event": "check", "verdict": "BLOCK"},
    ]}
    original = deepcopy(document)
    result = trace_metrics.analyze(document)
    assert result["workflow"] == expected
    assert result["rounds"] == [["read_article"]]
    assert result["rev"] == result["blocked"] == 1
    assert document == original


def test_current_events_do_not_become_zero_valued_legacy_metrics():
    assert trace_metrics.analyze({"reply": "current", "events": [
        {"node": "native", "event": "llm_done", "input": 100},
    ]}) is None
