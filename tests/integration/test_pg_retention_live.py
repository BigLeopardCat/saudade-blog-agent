"""Real checkpoint retention preserves recent data and rolls back a failed cleanup."""

import json
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone

import psycopg
import pytest
from psycopg import sql

from agent import checkpoint, idem_pg
from eval import pg_retention as retention

pytestmark = pytest.mark.enable_socket


@pytest.fixture
def seeded(checkpoint_database):
    with ExitStack() as resources:
        checkpoint.build_checkpointer(checkpoint_database, resources=resources)
        with psycopg.connect(checkpoint_database, autocommit=True) as conn:
            now = datetime.now(timezone.utc)
            for thread, age in [('old', 60), ('recent', 1)]:
                timestamp = (now - timedelta(days=age)).isoformat()
                conn.execute('INSERT INTO checkpoints (thread_id, checkpoint_ns, checkpoint_id,'
                             ' type, checkpoint, metadata) VALUES (%s, %s, %s, %s, %s, %s)',
                             (thread, '', '1', 'json', json.dumps({'ts': timestamp}), '{}'))
                conn.execute('INSERT INTO checkpoint_blobs (thread_id, checkpoint_ns, channel,'
                             ' version, type, blob) VALUES (%s, %s, %s, %s, %s, %s)',
                             (thread, '', 'messages', '1', 'json', b'{}'))
                conn.execute('INSERT INTO checkpoint_writes (thread_id, checkpoint_ns,'
                             ' checkpoint_id, task_id, idx, channel, type, blob)'
                             ' VALUES (%s, %s, %s, %s, %s, %s, %s, %s)',
                             (thread, '', '1', 'task', 0, 'messages', 'json', b'{}'))
                conn.execute(sql.SQL('INSERT INTO {} (key, tool, fp, status, ts)'
                                     ' VALUES (%s, %s, %s, %s, %s)').format(
                                         sql.Identifier(idem_pg.TABLE)),
                             (thread, 'tool', 'fingerprint', 'done', timestamp))
            yield conn, retention.scan(conn, retention.cutoff_of(30, now))


def row_counts(conn, thread):
    counts = [conn.execute(sql.SQL('SELECT count(*) FROM {} WHERE thread_id = %s').format(
        sql.Identifier(table)), (thread,)).fetchone()[0] for table in retention.THREAD_TABLES]
    counts.append(conn.execute(sql.SQL('SELECT count(*) FROM {} WHERE key = %s').format(
        sql.Identifier(idem_pg.TABLE)), (thread,)).fetchone()[0])
    return counts


def test_pg_retention_live(seeded):
    conn, plan = seeded
    assert [row['thread_id'] for row in plan['prune']] == ['old']
    assert not plan['undated'] and plan['ledger'] == {'exists': True, 'due': 1}
    result = retention.apply_plan(conn, plan)
    assert result['threads'] == 1 and result['ledger'] == 1
    assert row_counts(conn, 'old') == [0, 0, 0, 0]
    assert row_counts(conn, 'recent') == [1, 1, 1, 1]
    again = retention.scan(conn, plan['cutoff'])
    assert not again['prune'] and again['ledger']['due'] == 0
    assert retention.apply_plan(conn, again)['threads'] == 0


def test_retention_verification_failure_rolls_back_checkpoint_and_ledger(seeded, monkeypatch):
    conn, plan = seeded

    def reject_commit(*args):
        # Observe real deletes inside the transaction, then force verification failure.
        assert row_counts(conn, 'old') == [0, 0, 0, 0]
        raise RuntimeError('synthetic verification failure')

    monkeypatch.setattr(retention, '_assert_clean', reject_commit)
    with pytest.raises(RuntimeError, match='synthetic verification failure'):
        retention.apply_plan(conn, plan)
    assert row_counts(conn, 'old') == [1, 1, 1, 1]
    assert row_counts(conn, 'recent') == [1, 1, 1, 1]
