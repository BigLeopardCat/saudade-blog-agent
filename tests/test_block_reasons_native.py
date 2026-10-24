"""Structured block reasons used by the native transport."""

from agent.block_reasons import REASONS, block_reason_type, denied_workflows


def test_block_reason_catalog_is_closed_and_deterministic():
    assert REASONS
    assert all(isinstance(code, str)
               and isinstance(label, tuple)
               and len(label) == 2
               and isinstance(label[0], str)
               and isinstance(label[1], bool)
               for code, label in REASONS.items())
    assert block_reason_type("permission_denied")
    assert block_reason_type("consent_required")
    assert block_reason_type("unknown_reason") == ("unknown_reason", True)
    assert denied_workflows([]) == set()
