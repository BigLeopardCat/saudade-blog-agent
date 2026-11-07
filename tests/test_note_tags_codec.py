"""Public article tag identifiers are parsed without coercing malformed values."""

import pytest

from agent.article_labels import parse_tag_ids


@pytest.mark.parametrize("raw,expected", [
    (None, []), ("", []), ("1,2,1,,", [1, 2]),
    ([1, "2", 1, None, False, 0, -3], [1, 2]),
    (["12abc", "12.0", "1e2", "１２", "²", True, {}, []], []),
    ([" 12 ", "0012", "13"], [12, 13]),
])
def test_read_tag_ids_accepts_only_complete_positive_identifiers(raw, expected):
    assert parse_tag_ids(raw) == expected
