"""Retention selection and application outcomes; real lock/transaction tests run in CI."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from eval import pg_retention as retention
from eval.retention_manifest import KEEP_DAYS_CHECKPOINT

NOW = datetime(2026, 11, 6, tzinfo=timezone.utc)
CUTOFF = retention.cutoff_of(30, NOW)


def ids(rows):
    return [row["thread_id"] for row in rows]


def test_selection_protects_boundary_recent_and_any_unknown_activity():
    rows = [("old", 2, CUTOFF - timedelta(days=1), 0),
            ("edge", 1, CUTOFF, 0), ("recent", 1, NOW, 0),
            ("unknown", 2, None, 2), ("mixed", 2, CUTOFF - timedelta(days=2), 1)]
    plan = retention.plan_rows(rows, CUTOFF)
    assert ids(plan["prune"]) == ["old"]
    assert ids(plan["keep"]) == ["edge", "recent"]
    assert ids(plan["undated"]) == ["unknown", "mixed"]
    assert CUTOFF.tzinfo is not None
    assert retention.cutoff_of(7.5, NOW) == NOW - timedelta(days=7.5)
    assert retention.KEEP_DAYS_DEFAULT == KEEP_DAYS_CHECKPOINT


def test_thread_budget_selects_oldest_and_zero_does_no_work():
    rows = [("old", 1, CUTOFF - timedelta(days=1), 0),
            ("older", 1, CUTOFF - timedelta(days=2), 0)]
    plan = retention.plan_rows(rows, CUTOFF, 1)
    assert ids(plan["prune"]) == ["older"]
    assert ids(plan["deferred"]) == ["old"]
    assert retention.plan_rows(rows, CUTOFF, 0)["prune"] == []


@pytest.mark.parametrize("field,value", [
    ("run", {"id": "paused"}), ("turn", {"reply": "not committed"}),
    ("approval", {"jti": "pending"}), ("pending_confirm", {"id": "card"}),
    ("pending_action", {"jti": "pending"}), ("approved_result", {"reply": "pending"}),
])
def test_recoverable_state_is_not_expired(field, value):
    snapshot = SimpleNamespace(values={"run": None, field: value}, next=(), tasks=())
    assert retention._recoverable(Mock(get_state=lambda _: snapshot), "conv_1") == "recoverable"


@pytest.mark.parametrize("next_nodes,tasks", [(("commit",), ()), ((), (object(),))])
def test_framework_pending_work_is_protected(next_nodes, tasks):
    snapshot = SimpleNamespace(values={"run": None}, next=next_nodes, tasks=tasks)
    assert retention._recoverable(Mock(get_state=lambda _: snapshot), "conv_1") == "recoverable"


def test_unknown_schema_is_protected_and_completed_state_is_eligible():
    snapshot = SimpleNamespace(values={}, next=(), tasks=())
    store = Mock(get_state=lambda _: snapshot)
    assert retention._recoverable(store, "old-schema") == "unknown_state"
    snapshot.values = {"run": None, "messages": []}
    assert retention._recoverable(store, "conv_1") is None


def test_apply_reports_skips_and_only_counts_committed_deletions(monkeypatch):
    from agent.runtime import checkpoint_conversations

    monkeypatch.setattr(checkpoint_conversations, "CheckpointConversationStore", Mock())
    retire = Mock(side_effect=[{"skipped": "busy"}, {"rows": {"checkpoints": 2}},
                               {"skipped": "activity_changed"}])
    monkeypatch.setattr(retention, "_retire_thread", retire)
    conn = Mock()
    conn.transaction.return_value = SimpleNamespace()
    from contextlib import nullcontext
    conn.transaction.return_value = nullcontext()
    plan = {"prune": [{"thread_id": str(i)} for i in range(3)], "cutoff": CUTOFF,
            "ledger": {"exists": False}}
    result = retention.apply_plan(conn, plan)
    assert result["threads"] == 1 and result["rows"] == {"checkpoints": 2}
    assert result["skipped"] == [{"thread_id": "0", "reason": "busy"},
                                 {"thread_id": "2", "reason": "activity_changed"}]
    conn.execute.assert_not_called()


def test_missing_dsn_is_unevaluated_not_a_successful_scan(monkeypatch, capsys):
    monkeypatch.setattr(retention, "connect", Mock(side_effect=AssertionError("no DB allowed")))
    assert retention.main(["--dsn", ""]) == 0
    assert '"evaluated": false' in capsys.readouterr().out
