"""Archived terminal fallback evidence remains distinct from internal recovery."""

import pytest

from eval import golden_evaluation as golden


@pytest.mark.parametrize(("result", "expected"), [
    ({"resets": 1, "reset_scopes": ["all"], "resets_reasons": ["retry"]}, []),
    ({"resets": 1, "reset_scopes": ["text"], "fallback_reasons": ["fallback"]}, ["fallback"]),
    ({"resets": 2, "reset_scopes": ["all", "text"],
      "fallback_reasons": ["fallback"]}, ["fallback"]),
    ({"resets": 1, "resets_reasons": ["unknown archived reset"]}, ["unknown archived reset"]),
    ({"resets": 0}, []),
])
def test_archived_terminal_fallbacks_remain_failures(result, expected):
    assert golden.fallback_resets(result) == expected
    evidence = {"text": "reply", "commands": [], "tool_calls": [], "frames": [],
                "exec_rows": [], "exec_tools": [], **result}
    assert bool(golden.check_gold({"forbid_fallback": True}, evidence)) == bool(expected)
