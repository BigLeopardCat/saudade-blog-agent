"""Cross-worker client lifecycle CAS using the explicitly configured disposable CI database."""

import os
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest

from agent import idem, idem_pg
from tools.result import ok

pytestmark = pytest.mark.enable_socket


@pytest.fixture
def ledgers():
    dsn = os.environ.get('SAUDADE_TASK_INPUT_TEST_DSN')
    if not dsn:
        pytest.skip('SAUDADE_TASK_INPUT_TEST_DSN not set; production configuration is not used')
    import psycopg
    from psycopg import sql
    from psycopg_pool import ConnectionPool

    schema = 'client_test_' + uuid.uuid4().hex
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(schema)))
    try:
        with ConnectionPool(dsn, min_size=1, max_size=4, kwargs={
                'autocommit': True, 'options': '-c search_path=' + schema}) as pool:
            idem_pg.setup(pool)
            yield idem_pg.PostgresLedger(pool), idem_pg.PostgresLedger(pool)
    finally:
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(schema)))


def test_only_one_worker_can_replace_the_same_persisted_client_phase(ledgers):
    left, right = ledgers
    left.claim('operation', 'set_article_summary', 'a' * 32)
    left.finish('operation', ok('approved', meta={'phase': 'approved'}))
    previous = left.lookup('operation')[1]
    barrier = threading.Barrier(2)

    def advance(ledger, phase):
        barrier.wait(timeout=5)
        return ledger.replace('operation', previous, ok(phase, meta={'phase': phase}))

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(advance, ledger, phase) for ledger, phase in
                   ((left, 'verified'), (right, 'unverified'))]
        assert sorted(future.result(timeout=10) for future in futures) == [False, True]
    current = idem.replay_result(right.lookup('operation')[1])
    assert current.meta['phase'] in {'verified', 'unverified'}
    assert not left.replace('operation', previous, ok('stale', meta={'phase': 'approved'}))
    assert idem.replay_result(left.lookup('operation')[1]).meta == current.meta


def test_typed_summary_record_survives_cross_worker_phase_and_unicode_roundtrip(ledgers):
    import hashlib

    from agent.contracts.client import EditorObservation
    from agent.domains.article_summary_state import (
        ApprovedSummary,
        SummaryArguments,
        VerifiedSummary,
        restore,
        saved_result,
    )

    left, right = ledgers
    description = '已批准摘要😀\n第二行'
    operation = ApprovedSummary(operation_id='b' * 32, uid=7, conv=42, description=description,
        args=SummaryArguments(editor_id='editor', revision=0, request_id='summary',
            summary_sha256=hashlib.sha256(description.encode()).hexdigest(),
            previous_summary_sha256=hashlib.sha256(b'').hexdigest()))
    reference = operation.reference()
    key = reference['ledger_key']
    left.claim(key, reference['tool'], operation.operation_id)
    left.finish(key, saved_result(operation))
    previous = right.lookup(key)[1]
    waiting = restore(previous, reference).awaiting()
    assert right.replace(key, previous, saved_result(waiting))
    prior = left.lookup(key)[1]
    observed = waiting.verify(EditorObservation(editor_id='editor', revision=1, outcome='updated',
                                               description=description))
    assert left.replace(key, prior, saved_result(observed))
    restored = restore(right.lookup(key)[1], reference)
    assert isinstance(restored, VerifiedSummary) and restored.document.description == description
    assert not right.replace(key, prior, saved_result(waiting))
