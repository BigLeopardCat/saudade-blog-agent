"""Real saver deletion, transaction rollback and overlap with active runtime turns."""

from contextlib import ExitStack
from datetime import datetime, timedelta, timezone

import psycopg
import pytest
from langchain_core.messages import HumanMessage
from psycopg import sql

from agent import checkpoint, idem_pg
from agent.runtime import approval, turns
from eval import pg_retention as retention

pytestmark = pytest.mark.enable_socket


def config(thread):
    return {"configurable": {"thread_id": thread}}


@pytest.fixture
def seeded(checkpoint_database):
    with ExitStack() as resources:
        saver = checkpoint.build_checkpointer(checkpoint_database, resources=resources)
        graph = approval.graph(saver, agent=lambda state: {}, commit=lambda state: {})
        with psycopg.connect(checkpoint_database, autocommit=True) as conn:
            now = datetime.now(timezone.utc)
            for thread, age in [("conv_1", 60), ("conv_2", 1)]:
                graph.invoke({"messages": [HumanMessage(content=thread)], "run": None},
                             config(thread))
                timestamp = now - timedelta(days=age)
                conn.execute("UPDATE checkpoints SET checkpoint=jsonb_set(checkpoint,'{ts}',"
                             "to_jsonb(%s::text)) WHERE thread_id=%s",
                             (timestamp.isoformat(), thread))
                conn.execute(sql.SQL("INSERT INTO {} (key,tool,fp,status,ts) "
                                     "VALUES (%s,'tool','fingerprint','done',%s)").format(
                                         sql.Identifier(idem_pg.TABLE)), (thread, timestamp))
            conn.execute("INSERT INTO agent_write_idem (key,tool,fp,status,ts) "
                         "VALUES ('unknown','tool','fp','intent',%s)",
                         (now - timedelta(days=60),))
            yield conn, saver, graph, retention.scan(conn, retention.cutoff_of(30, now))


def row_counts(conn, thread):
    return [conn.execute(sql.SQL("SELECT count(*) FROM {} WHERE thread_id=%s").format(
        sql.Identifier(table)), (thread,)).fetchone()[0] for table in retention.THREAD_TABLES]


def test_public_saver_deletes_all_checkpoint_data_but_preserves_unknown_write_intents(seeded):
    conn, _, _, plan = seeded
    assert [row["thread_id"] for row in plan["prune"]] == ["conv_1"]
    recent = row_counts(conn, "conv_2")
    assert all(value > 0 for value in row_counts(conn, "conv_1"))
    assert plan["ledger"] == {"exists": True, "due": 1}
    result = retention.apply_plan(conn, plan)
    assert result["threads"] == 1 and result["ledger"] == 1
    assert row_counts(conn, "conv_1") == [0, 0, 0]
    assert row_counts(conn, "conv_2") == recent
    assert conn.execute("SELECT status FROM agent_write_idem WHERE key='unknown'").fetchone() == (
        "intent",)
    assert retention.apply_plan(conn, retention.scan(conn, plan["cutoff"]))["threads"] == 0


def test_busy_runtime_turn_is_skipped_without_releasing_its_lock(seeded):
    conn, saver, _, plan = seeded
    before = row_counts(conn, "conv_1")
    with turns.conversation_turn(saver, config("conv_1")):
        result = retention.apply_plan(conn, plan)
        assert result["threads"] == 0
        assert result["skipped"] == [{"thread_id": "conv_1", "reason": "busy"}]
        assert row_counts(conn, "conv_1") == before
    assert retention.apply_plan(conn, plan)["threads"] == 1


def test_candidate_restored_after_scan_is_rechecked_before_delete(seeded):
    conn, _, graph, plan = seeded
    graph.update_state(config("conv_1"), {"messages": [HumanMessage(content="restored")]},
                       as_node="commit")
    result = retention.apply_plan(conn, plan)
    assert result["threads"] == 0
    assert result["skipped"] == [{"thread_id": "conv_1", "reason": "activity_changed"}]
    assert graph.get_state(config("conv_1")).values["messages"][-1].content == "restored"


@pytest.mark.parametrize("field,value", [("run", {"id": "paused"}),
                                          ("pending_action", {"jti": "pending"})])
def test_old_recoverable_task_is_preserved_even_without_a_live_worker(seeded, field, value):
    conn, _, graph, plan = seeded
    graph.update_state(config("conv_1"), {field: value}, as_node="commit")
    conn.execute("UPDATE checkpoints SET checkpoint=jsonb_set(checkpoint,'{ts}',to_jsonb(%s::text))"
                 " WHERE thread_id='conv_1'", ((plan["cutoff"] - timedelta(days=1)).isoformat(),))
    result = retention.apply_plan(conn, plan)
    assert result["threads"] == 0
    assert result["skipped"] == [{"thread_id": "conv_1", "reason": "recoverable"}]
    assert graph.get_state(config("conv_1")).values[field] == value


def test_verification_failure_rolls_back_checkpoint_and_task_state(seeded, monkeypatch):
    conn, _, _, plan = seeded
    before = row_counts(conn, "conv_1")

    def reject(*_):
        assert row_counts(conn, "conv_1") == [0, 0, 0]
        raise RuntimeError("synthetic verification failure")

    monkeypatch.setattr(retention, "_assert_clean", reject)
    with pytest.raises(RuntimeError, match="synthetic verification failure"):
        retention.apply_plan(conn, plan)
    assert row_counts(conn, "conv_1") == before
    assert conn.execute("SELECT count(*) FROM agent_write_idem WHERE key='conv_1'").fetchone() == (
        1,)
