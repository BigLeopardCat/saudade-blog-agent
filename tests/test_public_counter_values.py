"""Public counts remain observed integer facts without coercion or truncation."""

import pytest

from tools.notes import _note_counts


def test_known_zero_is_preserved_and_missing_counts_are_omitted():
    assert _note_counts({"views": 0, "likes": 2}) == {"views": 0, "likes": 2}
    assert _note_counts({}) == {}


@pytest.mark.parametrize("value", [True, -1, 1.0, 1.5, "1", None,
                                  float("nan"), float("inf")])
def test_invalid_counter_values_do_not_become_facts(value):
    assert _note_counts({"views": value, "likes": 7}) == {"likes": 7}
