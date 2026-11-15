"""Durable dispatch markers distinguish untouched claims, cancellation and unknown effects."""

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing

import pytest

from agent.runtime import writes
from agent.runtime.turns import AgentCancelled
from eval.fixtures.ledger import Ledger
from tools.result import ok

GRANT = {"uid": 7, "conv": 42, "jti": "a" * 32}
ARGS = {"id": 19}
NAME = "operation"


class Operation:
    def __init__(self):
        self.calls = 0

    def invoke(self, args, config=None):
        self.calls += 1
        time.sleep(0.01)
        return ok("completed")


def test_crash_after_claim_can_resume_and_atomic_dispatch_elects_one_worker(tmp_path):
    path = str(tmp_path / "dispatch.sqlite3")
    key = writes.operation_key(GRANT, NAME, ARGS)
    with closing(Ledger(path)) as ledger:
        assert ledger.claim(key, NAME, GRANT["jti"], pending=True)[0] == "new"
    operation = Operation()

    def execute(_):
        with closing(Ledger(path)) as ledger:
            return writes.execute_once(GRANT, NAME, ARGS, {}, ledger, {NAME: operation})

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(execute, range(8)))
    assert operation.calls == 1
    with closing(Ledger(path)) as ledger:
        assert ledger.lookup(key)[0] == "done"


def test_cancellation_between_claim_and_dispatch_is_persisted_and_not_retried(tmp_path):
    stopped = threading.Event()

    class CancelOnClaim(Ledger):
        def claim(self, *args, **kwargs):
            result = super().claim(*args, **kwargs)
            stopped.set()
            return result

    operation = Operation()
    path = str(tmp_path / "cancelled.sqlite3")
    cfg = {"configurable": {"stop_event": stopped}}
    with closing(CancelOnClaim(path)) as ledger:
        with pytest.raises(AgentCancelled):
            writes.execute_once(GRANT, NAME, ARGS, cfg, ledger, {NAME: operation})
        assert ledger.lookup(writes.operation_key(GRANT, NAME, ARGS))[0] == "cancelled"
    with closing(Ledger(path)) as ledger:
        result = writes.execute_once(GRANT, NAME, ARGS, {}, ledger, {NAME: operation})
    assert operation.calls == 0 and result.meta["reason"] == "write_cancelled_before_dispatch"


def test_legacy_intent_never_gains_new_recovery_or_dispatch_authority(tmp_path):
    class Recoverable(Operation):
        def recover(self, *args, **kwargs):
            pytest.fail("Legacy intent cannot prove use of the keyed host API")

    operation = Recoverable()
    with closing(Ledger(str(tmp_path / "legacy.sqlite3"))) as ledger:
        key = writes.operation_key(GRANT, NAME, ARGS)
        ledger.claim(key, NAME, GRANT["jti"])
        result = writes.execute_once(GRANT, NAME, ARGS, {}, ledger, {NAME: operation})
        assert ledger.lookup(key)[0] == "intent"
    assert operation.calls == 0 and result.meta["reason"] == "write_outcome_unknown"
