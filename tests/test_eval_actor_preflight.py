"""The canonical online runner requires explicit actors before model execution."""

from types import SimpleNamespace

import pytest

from eval.online_baseline import actor_preflight


@pytest.mark.parametrize("identity", [None, 0, -1, True, "123", "not-an-id"])
def test_invalid_actor_is_rejected_without_any_transport_call(identity):
    calls = []
    transport = SimpleNamespace(profile=lambda *args: calls.append(args))
    with pytest.raises(ValueError, match="positive account ID"):
        actor_preflight([{"identity": "admin"}], transport, {"admin": identity})
    assert calls == []


def test_explicit_actors_keep_separate_roles_and_planned_turn_allowances():
    calls = []

    def profile(uid, roles):
        calls.append(("profile", uid, roles))
        return {"id": uid}

    def allowance(uid, required):
        calls.append(("allowance", uid, required))
        return {"remaining": 10}

    transport = SimpleNamespace(host=True, profile=profile, check_turn_allowance=allowance)
    profiles, allowances, _ = actor_preflight([
        {"identity": "admin", "rounds": [{}, {}]}, {"identity": "user"},
    ], transport, {"admin": 123, "user": 456})
    assert profiles == {"admin": {"id": 123}, "user": {"id": 456}}
    assert set(allowances) == {"admin", "user"}
    assert calls == [("profile", 123, {"admin", "superadmin"}), ("allowance", 123, 2),
                     ("profile", 456, {"user"}), ("allowance", 456, 1)]
