"""Archived target provenance is decoded locally without permissive runtime IDs."""

import sys
from pathlib import Path

import pytest

from eval import golden_evaluation as golden


def evidence(ids, target=7):
    return {"text": "fixture", "commands": [], "tool_calls": [], "exec_rows": [],
            "ledger_frames": [{"ids": ids}],
            "confirm_payloads": [{"specs": [{"tool": "audit_board_comment",
                                               "args": {"talk_id": target}}]}]}


@pytest.mark.parametrize(("ids", "target", "passed"), [
    (["talkId:7"], 7, True),
    (["talkId:007"], "7", True),
    (["commentId:7"], 7, False),
    (["unknown:7"], 7, False),
    (["talkId:7:8"], 7, False),
    (["talkId:7 OR 1=1"], 7, False),
    (["talkId:７"], 7, False),
    (["talkId:0"], 0, False),
    (["talkId:2147483648"], 2147483648, False),
    (["talkId:7"], True, False),
    ([], 7, False),
])
def test_archived_target_ids_are_strict_and_family_scoped(monkeypatch, ids, target, passed):
    eval_directory = Path(golden.__file__).parent
    monkeypatch.setattr(sys, "path", [part for part in sys.path
                                      if Path(part or ".").resolve() != eval_directory])
    monkeypatch.delenv("GOLDEN_NO_TRACE", raising=False)
    failures = golden.check_gold({"require_card_targets_from_ledger": True}, evidence(ids, target))
    assert (not failures) is passed


def test_missing_trace_evidence_is_unevaluated(monkeypatch):
    monkeypatch.setenv("GOLDEN_NO_TRACE", "1")
    failures = golden.check_gold({"require_card_targets_from_ledger": True},
                                  evidence(["talkId:7"]))
    assert failures and any("未评估" in failure for failure in failures)
