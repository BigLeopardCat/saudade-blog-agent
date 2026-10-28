"""Confirmed writes retain intent, replay evidence and isolate signed operations."""

from contextlib import closing

import pytest

from agent import idem
from agent.runtime import writes
from tools.result import ok


class Operation:
    def __init__(self, before_call=lambda: None, fail=False):
        self.calls = 0
        self.before_call = before_call
        self.fail = fail

    def invoke(self, args, config=None):
        self.before_call()
        self.calls += 1
        if self.fail:
            raise RuntimeError("remote write succeeded but its response was lost")
        return ok("Applied once", meta={"changed": True, "target": args["id"]})


GRANT = {"uid": 7, "conv": 42, "jti": "a" * 32}
ARGS = {"id": 19}
NAME = "confirmed_operation"


def invoke(ledger, tool, grant=GRANT, name=NAME, args=ARGS):
    return writes.execute_once(grant, name, args, {}, ledger, {name: tool})


def test_write_intent_survives_process_restart_and_preserves_verified_result(tmp_path):
    path = str(tmp_path / "writes.sqlite3")
    with closing(idem.Ledger(path)) as ledger:
        def check_intent():
            key = writes.operation_key(GRANT, NAME, ARGS)
            state, _ = ledger.claim(key, NAME, GRANT["jti"])
            assert state == "intent", "external invocation must follow durable intent"

        tool = Operation(before_call=check_intent)
        first = invoke(ledger, tool)
    with closing(idem.Ledger(path)) as reopened:
        replay = invoke(reopened, tool)
    assert tool.calls == 1
    assert str(first) == str(replay)
    assert first.kind == replay.kind == "ok"
    assert first.meta == replay.meta == {"changed": True, "target": 19}


def test_unknown_remote_outcome_does_not_reissue_the_write_after_restart(tmp_path):
    path = str(tmp_path / "unknown.sqlite3")
    tool = Operation(fail=True)
    with closing(idem.Ledger(path)) as ledger:
        with pytest.raises(RuntimeError, match="response was lost"):
            invoke(ledger, tool)
    with closing(idem.Ledger(path)) as reopened:
        result = invoke(reopened, tool)
    assert tool.calls == 1
    assert result.meta["reason"] == "write_outcome_unknown"
    assert "changed" not in result.meta


def test_unavailable_ledger_never_reaches_the_remote_operation():
    class UnavailableLedger:
        def claim(self, *args, **kwargs):
            raise OSError("storage unavailable")

    tool = Operation()
    with pytest.raises(OSError):
        invoke(UnavailableLedger(), tool)
    assert tool.calls == 0


@pytest.mark.parametrize("grant,name,args", [
    ({**GRANT, "uid": 8}, NAME, ARGS),
    ({**GRANT, "conv": 43}, NAME, ARGS),
    ({**GRANT, "jti": "b" * 32}, NAME, ARGS),
    (GRANT, "different_operation", ARGS),
    (GRANT, NAME, {"id": 20}),
])
def test_distinct_signed_operations_are_not_silently_deduplicated(tmp_path, grant, name, args):
    with closing(idem.Ledger(str(tmp_path / "isolated.sqlite3"))) as ledger:
        tool = Operation()
        invoke(ledger, tool)
        invoke(ledger, tool, grant, name, args)
        invoke(ledger, tool, grant, name, args)
    assert tool.calls == 2
