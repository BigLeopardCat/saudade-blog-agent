"""Exercise dispatch election and unsent cancellation against disposable PostgreSQL."""

import os
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest

from agent.idem_pg import PostgresLedger, setup
from agent.runtime import writes
from agent.runtime.turns import AgentCancelled
from tools.result import ok

pytestmark = pytest.mark.enable_socket
GRANT = {"uid": 7, "conv": 42, "jti": "a" * 32}
ARGS = {"id": 19}


@pytest.fixture
def ledger_pool():
    dsn = os.environ.get("SAUDADE_TASK_INPUT_TEST_DSN")
    if not dsn:
        if os.environ.get("SAUDADE_REQUIRE_CKPT_TEST") == "1":
            pytest.fail("disposable PostgreSQL DSN is required")
        pytest.skip("explicit disposable DSN not set")
    import psycopg
    from psycopg import sql
    from psycopg_pool import ConnectionPool

    schema = "write_dispatch_" + uuid.uuid4().hex
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    try:
        with ConnectionPool(dsn, min_size=1, max_size=4, timeout=5, kwargs={
            "autocommit": True, "options": f"-c search_path={schema} -c statement_timeout=5000",
        }) as pool:
            pool.wait(timeout=5)
            setup(pool)
            yield pool
    finally:
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


def test_only_one_postgres_worker_can_dispatch_a_previously_unstarted_claim(ledger_pool):
    key = writes.operation_key(GRANT, "op", ARGS)
    ledger = PostgresLedger(ledger_pool)
    assert ledger.claim(key, "op", GRANT["jti"], pending=True)[0] == "new"
    calls = []

    class Operation:
        def invoke(self, args, config=None):
            assert PostgresLedger(ledger_pool).lookup(key)[0] == "dispatched"
            calls.append(1)
            time.sleep(0.02)
            return ok("done")

    operation = Operation()

    def execute(_):
        return writes.execute_once(GRANT, "op", ARGS, {},
                                   PostgresLedger(ledger_pool), {"op": operation})

    with ThreadPoolExecutor(max_workers=6) as executor:
        list(executor.map(execute, range(12)))
    assert len(calls) == 1 and PostgresLedger(ledger_pool).lookup(key)[0] == "done"


def test_postgres_cancellation_after_claim_is_durable_and_never_dispatched(ledger_pool):
    stopped = threading.Event()

    class CancelAfterClaim(PostgresLedger):
        def claim(self, *args, **kwargs):
            claimed = super().claim(*args, **kwargs)
            stopped.set()
            return claimed

    class Operation:
        def invoke(self, args, config=None):
            pytest.fail("cancelled operation must not be called")

    with pytest.raises(AgentCancelled):
        writes.execute_once(GRANT, "op", ARGS, {"configurable": {"stop_event": stopped}},
                            CancelAfterClaim(ledger_pool), {"op": Operation()})
    key = writes.operation_key(GRANT, "op", ARGS)
    assert PostgresLedger(ledger_pool).lookup(key)[0] == "cancelled"
