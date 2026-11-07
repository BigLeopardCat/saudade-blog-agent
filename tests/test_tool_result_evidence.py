"""Write metadata cannot convert missing or untyped state into observed success/no-op."""

import pytest

from tools.result import fact, ok, tgt


@pytest.mark.parametrize("value", [None, "false", "true", 0, 1, {}, []])
def test_write_state_requires_explicit_boolean_evidence(value):
    with pytest.raises(TypeError, match="observed boolean"):
        fact("favorite_add", changed=value)


def test_noop_state_retains_observed_target_and_readback_evidence():
    target = tgt("note", 19)
    result = ok("已经收藏，无需重复操作。", meta=fact(
        "favorite_add", changed=False, target=target, before="saved", after="saved",
        evidence="current-owner-favorites"))
    assert result.kind == "ok"
    assert result.meta == {"op": "favorite_add", "changed": False, "target": target,
                           "before": "saved", "after": "saved",
                           "evidence": "current-owner-favorites"}
