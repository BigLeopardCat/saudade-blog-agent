"""Task reports join real request boundaries without treating reservations as billing."""

import asyncio
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from _memory_support import Capture, config, runtime
from langchain_core.messages import AIMessageChunk, HumanMessage
from langgraph.checkpoint.memory import InMemorySaver

from agent.runtime.control import AgentCancelled, RequestControl
from agent.runtime.jobs import RequestJobs
from eval import task_metrics
from utils.trace import finish_trace, start_trace


def trace(tmp_path, ident, *, task="task", before=0, after=100, phase="responded", **changes):
    def spent(tokens):
        return {"tokens": tokens, "model_calls": tokens // 100, "tool_calls": 0}

    doc = {"trace_id": ident, "started_at": "2026-10-29T10:00:00", "duration_s": 1,
           "user_id": 7, "input": {"source": "production", "conversation_id": 42,
                "principal_source": "assertion", "principal_role": "user",
                "runtime": {"name": "native", "build_sha": "build", "model": "fixture"}},
           "events": [{"node": "budget", "event": "task_restored", "task_id": task,
                       **spent(before)},
                      {"node": "native", "event": "turn_state", "phase": phase},
                      {"node": "budget", "event": "task_usage", "task_id": task,
                       "spent": spent(after), "request_spent": spent(after - before)},
                      {"node": "native", "event": "llm_start"},
                      {"node": "native", "event": "llm_done", "input": 80,
                       "output": 20, "cache_read": 64}]}
    doc.update(changes)
    path = tmp_path / f"{ident}.json"
    path.write_text(json.dumps(doc))
    return path


def test_resume_and_browser_wait_form_one_task_with_distinct_active_and_elapsed_time(tmp_path):
    first = trace(tmp_path, "first", phase="awaiting_browser", duration_s=2)
    second = trace(tmp_path, "second", before=100, after=200,
                   started_at="2026-10-29T10:01:00", duration_s=3)
    report = task_metrics.collect([second, first, first])
    row = report["tasks"][0]
    assert report["duplicate_traces"] == 1
    assert row["requests"] == row["calls"] == 2
    assert row["active_request_s"] == 5 and row["elapsed_s"] == 63
    assert row["phase_counts"] == {"awaiting_browser": 1, "responded": 1}
    assert row["budget_history_complete"] and row["budget_spent"]["tokens"] == 200
    assert row["in"] == 160 and row["out"] == 40 and row["cache_hit_rate"] == 0.8
    assert row["provider_usage_complete"]


def test_missing_earlier_attempt_or_snapshot_is_not_a_complete_task_history(tmp_path):
    row = task_metrics.collect([trace(tmp_path, "resumed", before=100, after=200)])["tasks"][0]
    assert not row["budget_history_complete"] and row["in"] == 80
    assert row["observed_usage_complete"] and not row["provider_usage_complete"]
    path = trace(tmp_path, "legacy")
    doc = json.loads(path.read_text())
    doc["events"] = [e for e in doc["events"] if e["event"] != "task_usage"]
    path.write_text(json.dumps(doc))
    row = task_metrics.collect([path])["tasks"][0]
    assert not row["budget_history_complete"] and row["budget_spent"] is None


def test_identity_conflicts_and_missing_bindings_never_merge_unrelated_requests(tmp_path):
    a, b = trace(tmp_path, "a"), trace(tmp_path, "b", user_id=8)
    c = trace(tmp_path, "c", events=[])
    conflict = trace(tmp_path, "conflict", trace_id="a", reply="different")
    report = task_metrics.collect([a, b, c, conflict])
    assert report["conflicting_trace_ids"] == ["a"]
    assert report["unassigned_requests"] == 1
    assert len(report["tasks"]) == 1 and report["tasks"][0]["user_id"] == 8


def test_cancelled_framework_call_and_resume_have_final_snapshots_before_trace_closure(tmp_path):
    class Interrupted(Capture):
        def stream(self, messages, **kwargs):
            self.prompts.append(list(messages))
            if len(self.prompts) == 1:
                raise AgentCancelled()
            yield AIMessageChunk(content="continued", usage_metadata={
                "input_tokens": 20, "output_tokens": 5, "total_tokens": 25,
                "input_token_details": {"cache_read": 16}})

    saver, model = InMemorySaver(), Interrupted()
    paths = []

    async def attempt(executor, ident, state, cancelled=False):
        start_trace(ident, 7, "conv_42", {"source": "test", "conversation_id": 42,
                    "principal_source": "assertion", "principal_role": "user"},
                    dir=str(tmp_path), name=ident, by_day=False)
        jobs = RequestJobs(asyncio.get_running_loop(), lambda: None,
            control=RequestControl(threading.Event(), time.monotonic() + 10),
            on_complete=lambda: paths.append(finish_trace(ident,
                "cancelled" if cancelled else "producer_done", 1)))
        selected = runtime(saver, model)
        future = jobs.submit(executor, list, selected.stream_events(state, config()))
        try:
            if cancelled:
                with pytest.raises(AgentCancelled):
                    await future
            else:
                await future
        finally:
            jobs.close()
            await jobs.wait_closed()
        return selected.store.load(config()).run

    async def scenario(executor):
        task = await attempt(executor, "interrupted", {
            "messages": [HumanMessage(content="run fixture")], "task_id": "same-task"}, True)
        await attempt(executor, "resumed", {"resume_task": task["id"]})

    with ThreadPoolExecutor(max_workers=1) as executor:
        asyncio.run(scenario(executor))
    assert len(paths) == 2
    row = task_metrics.collect(paths)["tasks"][0]
    assert row["requests"] == row["budget_spent"]["model_calls"] == 2
    assert row["budget_history_complete"]
    assert row["unknown_attempts"] == 1 and not row["provider_usage_complete"]
    assert row["in"] == 20 and row["out"] == 5
    assert row["budget_spent"]["tokens"] > 25
    for path in paths:
        events = json.loads(Path(path).read_text())["events"]
        assert sum(e["event"] == "task_usage" for e in events) == 1


def test_mixed_timestamp_clocks_do_not_report_elapsed_time(tmp_path):
    a = trace(tmp_path, "first", started_at="2026-10-29T10:00:00+08:00")
    b = trace(tmp_path, "second", before=100, after=200)
    row = task_metrics.collect([a, b])["tasks"][0]
    assert row["elapsed_s"] is None and row["active_request_s"] == 2


def test_browser_interrupt_has_its_own_phase_without_claiming_a_completed_reply(monkeypatch):
    from agent.runtime.results import TurnResult
    from agent.runtime.trace_context import outcome

    seen = []
    monkeypatch.setattr("agent.runtime.trace_context.record",
                        lambda node, event, **values: seen.append(values))
    outcome(TurnResult("", {"client_action": {"command_id": "command"}}))
    assert seen == [{"phase": "awaiting_browser", "receipt_count": 0}]
