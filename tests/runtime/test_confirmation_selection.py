"""Selections can only reduce signed operation units, and omitted work stays unapproved."""

import secrets

import pytest

from agent import confirm


@pytest.fixture
def grant(monkeypatch):
    monkeypatch.setattr(confirm.settings, "jwt_secret", secrets.token_urlsafe(32))
    specs = [{"tool": "delete_dashboard_todo", "args": {"item_id": value}} for value in range(1, 6)]
    token = confirm.sign(7, 42, "fixture", specs, units=[[0, 1], [2], [3, 4]])
    return confirm.verify(token, 7, 42)


def test_multi_selection_preserves_linked_units_and_remaps_groups(grant):
    selected, error = confirm.narrow(grant, "pick:2,0")
    assert not error
    assert [item["args"]["item_id"] for item in selected["specs"]] == [1, 2, 4, 5]
    assert selected["units"] == [[0, 1], [2, 3]]
    assert confirm.units_of(selected) == [[0, 1], [2, 3]]
    assert selected["selection"] == {
        "selected_units": 2,
        "total_units": 3,
        "unselected": "retained_not_queued",
    }
    assert len(grant["specs"]) == 5


@pytest.mark.parametrize(
    "pick",
    [
        "pick:",
        "pick:0,0",
        "pick:3",
        "pick:-1",
        "pick:1.0",
        "pick:0,99",
        "pick:0, 1",
        "yes",
        "pick:" + ",".join(["1"] * 51),
    ],
)
def test_invalid_or_expanding_selection_is_rejected(grant, pick):
    selected, error = confirm.narrow(grant, pick)
    assert selected is None and error


def test_all_and_single_selection_keep_existing_meaning(grant):
    assert confirm.narrow(grant, "")[0] is grant
    selected, error = confirm.narrow(grant, "pick:1")
    assert not error and selected["specs"] == [grant["specs"][2]]
