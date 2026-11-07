"""Host DTO IDs cannot be inferred from prose or coerced from unrelated values."""

import pytest

from agent.identifiers import normalize_target_id


@pytest.mark.parametrize("value,expected", [(7, 7), (" 007 ", 7), (2_147_483_647, 2_147_483_647),
                                           ("0" * 5000 + "1", 1)])
def test_record_identifiers_preserve_valid_host_coordinates(value, expected):
    assert normalize_target_id(value) == expected


@pytest.mark.parametrize("value", [True, False, 7.0, 0, -1, 2_147_483_648, "²", "７",
                                  "#7", "userId=7", "id:7", "7abc", "9" * 5000, None, {}])
def test_record_identifiers_reject_malformed_or_unbounded_values(value):
    assert normalize_target_id(value) is None
