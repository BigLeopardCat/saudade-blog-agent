"""Daily accounting separates current requests, archived diagnostics and unknown traces."""

import json

import pytest

from eval import daily_metrics as dm
from eval import request_metrics


def archived_calls(name, args, verdict=None, noop=False):
    events = [{"node": "execute", "event": "call", "name": name, "args": args}]
    if noop:
        events.append({"node": "execute", "event": "noop"})
    if verdict is not None:
        events.append({"node": "execute", "event": "check", "verdict": verdict})
    return events


@pytest.mark.parametrize(
    "first,last,expected",
    [
        (archived_calls("w", {"a": 1}, "BLOCK"),
         archived_calls("w", {"a": 1}, "BLOCK"), []),
        (archived_calls("w", {"a": 1}, "PASS"),
         archived_calls("w", {"a": 1}, "PASS", noop=True), []),
        (archived_calls("w", {"a": 1}, "PASS"),
         archived_calls("w", {"a": 1}, "PASS"), [("w", '{"a": 1}', 2)]),
        (archived_calls("w", {"a": 1}, "PASS"),
         archived_calls("w", {"a": 2}, "PASS"), []),
        (archived_calls("w", {"a": 1}), archived_calls("w", {"a": 1}), []),
        (archived_calls("w", {"a": 1, "b": 2}, "PASS"),
         archived_calls("w", {"b": 2, "a": 1}, "PASS"), [("w", '{"a": 1, "b": 2}', 2)]),
    ],
    ids=["blocked", "noop", "duplicate", "different-args", "unknown", "argument-order"],
)
def test_archived_duplicate_writes_require_matching_successful_receipts(first, last, expected):
    assert dm.dup_writes(first + last) == expected


def test_empty_corpus_does_not_invent_a_zero_cache_hit_rate():
    line = dm.render([], 7)
    assert "traces=0" in line and "模型缓存=量不到" in line


def test_archived_workflow_alerts_need_enough_samples_and_keep_the_actual_frequency(tmp_path):
    paths = []
    for index, (workflow, fallback) in enumerate([
        ("noise", True), ("noise", True),
        ("roster", True), ("roster", False), ("roster", False),
    ]):
        events = [{"node": "planner", "event": "decision", "workflow": workflow,
                   "tools": ["t({})"], "round": 0}]
        if fallback:
            events.append({"node": "gate", "event": "fallback", "workflow": workflow})
        path = tmp_path / f"20261016T00000{index}_1_fixture.json"
        path.write_text(json.dumps({"events": events, "reply": "x"}))
        paths.append(str(path))
    counts = dm.collect(paths)
    assert counts["per_workflow"]["noise"] == [2, 2]
    assert counts["per_workflow"]["roster"] == [3, 1]
    assert counts["graph"] == counts["total"] == 5 and counts["unread"] == 0
    line = dm.render(paths, 1)
    assert "roster(1/3)" in line and "noise" not in line
    assert "graph写重复=0" in line and "×" not in line.split("写重复")[1].split("|")[0]


@pytest.mark.parametrize("runtime", ["langchain", "native"])
def test_current_and_archived_runtime_labels_need_no_planner_events(tmp_path, runtime):
    trace = tmp_path / "20261028T120000_7_fixture.json"
    trace.write_text(json.dumps({"input": {"runtime": {"name": runtime}},
                                "events": [{"node": "native", "event": "llm_done",
                                            "input": 100, "cache_read": 75, "output": 10}]}))
    unknown = tmp_path / "20261028T120001_7_unknown.json"
    unknown.write_text(json.dumps({"events": []}))
    counts = dm.collect([str(trace), str(unknown)])
    assert counts[runtime] == counts["unread"] == 1
    assert counts["total"] == 2 and counts["graph"] == counts["dup"] == 0
    line = dm.render([str(trace)], 1)
    assert f"{runtime}=1" in line and "未识别=0" in line and "75.0%" in line
    assert request_metrics.collect([str(trace)])["groups"][0]["path"] == runtime


def test_mixed_runtime_populations_and_invalid_traces_remain_separate(tmp_path):
    paths = []
    for index, runtime in enumerate(["langchain", "native", "hybrid"]):
        path = tmp_path / f"20261028T12000{index}_7_fixture.json"
        path.write_text(json.dumps({"input": {"runtime": {"name": runtime, "arm": runtime}},
                                    "events": []}))
        paths.append(str(path))
    broken = tmp_path / "20261028T120003_7_broken.json"
    broken.write_text("not json")
    paths.append(str(broken))
    counts = dm.collect(paths)
    assert counts["total"] == 3 and counts["unread"] == 1
    assert counts["langchain"] == counts["native"] == counts["hybrid"] == 1
    assert counts["graph"] == counts["handoff"] == 0
