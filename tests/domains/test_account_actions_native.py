"""Account approvals preserve exact targets, policy checks and one-write semantics."""

import copy
import hashlib
import json
import secrets

import httpx
import pytest
from _own_support import config
from pydantic import ValidationError

from agent import confirm, idem
from agent.domains.account_actions import preparation, transactions
from agent.principal import SOURCE_ASSERTION, Principal
from agent.runtime import writes
from tools import base


def admin_config(**values):
    return config(principal=Principal(uid=7, role="admin", source=SOURCE_ASSERTION), **values)


def test_role_intent_uses_json_primitives_at_the_checkpoint_boundary():
    from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

    from agent.domains.account_actions import targets

    intent = targets.intent({"action": "set_role", "target": {"id": 11}, "role": "zako"})
    assert type(intent["role"]) is str
    serializer = JsonPlusSerializer(allowed_msgpack_modules=None)
    restored = serializer.loads_typed(serializer.dumps_typed(intent))
    assert restored == intent and type(restored["role"]) is str


@pytest.fixture
def accounts(monkeypatch):
    state = {
        "rows": {
            11: {"id": 11, "username": "测试账号", "role": "user", "status": 0, "mutedUntil": None},
            12: {
                "id": 12,
                "username": "测试账号甲",
                "role": "user",
                "status": 1,
                "mutedUntil": None,
            },
        },
        "writes": 0,
        "notifications": 0,
        "revocations": 0,
        "requests": [],
        "revision": 0,
    }

    def plan(intent):
        target = intent["target"]
        found = [
            row
            for row in state["rows"].values()
            if (target["id"] is not None and row["id"] == target["id"])
            or (target["username"] is not None and row["username"] == target["username"])
        ]
        if len(found) != 1:
            return None
        before = copy.deepcopy(found[0])
        after = copy.deepcopy(before)
        action = intent["action"]
        if action in {"freeze", "unfreeze"}:
            after["status"] = int(action == "freeze")
        elif action == "set_role":
            after["role"] = intent["role"]
        else:
            after["mutedUntil"] = intent["mutedUntil"] if action == "mute" else None
        changed = before != after or action == "freeze"
        revoke = action == "freeze" or (action == "set_role" and changed)
        version = hashlib.sha256(
            json.dumps([intent, before, after, state["revision"]], sort_keys=True).encode()
        ).hexdigest()
        return {
            "version": version,
            "before": before,
            "after": after,
            "changed": changed,
            "revokeSessions": revoke,
            "notificationCount": int(changed),
        }

    def handle(request):
        state["requests"].append(request)
        payload = json.loads(request.content)
        intent = payload if request.url.path.endswith("/prepare") else payload["intent"]
        if state.get("denied") or (
            state.get("commit_denied") and not request.url.path.endswith("/prepare")
        ):
            return httpx.Response(200, json={"code": 403, "message": "权限已撤销", "data": {}})
        prepared = plan(intent)
        if prepared is None:
            return httpx.Response(
                200, json={"code": 409, "message": "目标不唯一或不存在", "data": {}}
            )
        if request.url.path.endswith("/prepare"):
            if state.get("wrong_plan"):
                prepared["after"]["username"] = "另一个账号"
            if state.get("sensitive_plan"):
                prepared["before"]["password"] = secrets.token_urlsafe(24)
            return httpx.Response(200, json={"code": 200, "data": prepared})
        if state.get("race"):
            state["revision"] += 1
            prepared = plan(intent)
        if payload["version"] != prepared["version"]:
            return httpx.Response(200, json={"code": 409, "message": "状态已变化", "data": {}})
        state["writes"] += 1
        state["rows"][prepared["after"]["id"]] = copy.deepcopy(prepared["after"])
        state["notifications"] += prepared["notificationCount"]
        state["revocations"] += int(prepared["revokeSessions"])
        if state.get("lost"):
            raise httpx.ReadTimeout("response lost after commit")
        result = {
            "changed": prepared["changed"],
            "beforeVersion": payload["version"],
            "state": prepared,
        }
        if state.get("wrong_result"):
            result["state"]["notificationCount"] += 1
        return httpx.Response(200, json={"code": 200, "data": result})

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        monkeypatch.setattr(base, "_client", client)
        monkeypatch.setattr(confirm.settings, "jwt_secret", secrets.token_urlsafe(32))
        yield state


def prepare(action, **args):
    return getattr(preparation, "prepare_account_" + action).invoke(args, config=admin_config())


def grant(prepared):
    return confirm.verify(prepared.execution["pending_confirm"]["token"], 7, 42)


@pytest.mark.parametrize(
    "action,args,tool,field,value",
    [
        ("freeze", {"account_id": 11}, "freeze_account", "status", 1),
        ("unfreeze", {"account_id": 12}, "unfreeze_account", "status", 0),
        ("role", {"username": "测试账号", "role": "zako"}, "set_account_role", "role", "zako"),
        (
            "mute",
            {"account_id": 11, "permanent": True},
            "account_mute",
            "mutedUntil",
            "9999-12-31T23:59:59",
        ),
        ("unmute", {"account_id": 11}, "account_unmute", "mutedUntil", None),
    ],
)
def test_prepare_is_read_only_and_replay_applies_exact_change_once(
    accounts, tmp_path, action, args, tool, field, value
):
    if action == "unmute":
        accounts["rows"][11]["mutedUntil"] = "9999-12-31T23:59:59"
    original = copy.deepcopy(accounts["rows"])
    prepared = prepare(action, **args)
    signed = grant(prepared)
    assert signed["executor"] == "account_actions" and signed["specs"][0]["tool"] == tool
    assert set(signed["specs"][0]["args"]) == {"version", "intent"}
    assert accounts["rows"] == original and accounts["writes"] == 0
    assert all(request.url.path.endswith("/prepare") for request in accounts["requests"])
    assert "#" + str(args.get("account_id", 11)) in prepared.reply
    ledger = idem.Ledger(str(tmp_path / "once.sqlite3"))
    for _ in range(2):
        result = transactions.execute(signed, admin_config(), None, ledger=ledger)
        assert len(result["receipts"]) == 1 and not result["blocked"]
    target = args.get("account_id", 11)
    expected = {**original[target], field: value}
    assert accounts["rows"][target] == expected
    assert accounts["writes"] == 1 and accounts["notifications"] == 1
    assert accounts["revocations"] == int(action in {"freeze", "role"})


@pytest.mark.parametrize(
    "args", [{}, {"account_id": 11, "username": "测试账号"}, {"username": " "}]
)
def test_invalid_target_never_reaches_service(accounts, args):
    result = prepare("freeze", **args)
    assert result.kind == "invalid_args" and not accounts["requests"]


def test_target_name_is_exact_and_ambiguous_names_require_id(accounts):
    assert prepare("freeze", username="测试").kind != "ok"
    assert not accounts["writes"]
    accounts["rows"][12]["username"] = "测试账号"
    result = prepare("freeze", username="测试账号")
    assert not getattr(result, "execution", {}).get("pending_confirm")
    assert (
        grant(prepare("freeze", account_id=11))["specs"][0]["args"]["intent"]["target"]["id"] == 11
    )
    assert not accounts["writes"]


@pytest.mark.parametrize(
    "action,args", [("role", {"role": "user"}), ("unmute", {}), ("unfreeze", {})]
)
def test_noop_has_no_card_or_side_effect(accounts, action, args):
    prepared = prepare(action, account_id=11, **args)
    assert prepared.execution["noop_note"] and not prepared.execution.get("pending_confirm")
    assert not accounts["writes"] and not accounts["notifications"] and not accounts["revocations"]


def test_repeated_freeze_still_confirms_session_revocation(accounts, tmp_path):
    prepared = prepare("freeze", account_id=12)
    signed = grant(prepared)
    result = transactions.execute(
        signed, admin_config(), None, ledger=idem.Ledger(str(tmp_path / "refreeze.sqlite3"))
    )
    assert result["receipts"] and accounts["revocations"] == 1
    assert accounts["rows"][12]["status"] == 1


def test_temporary_mute_freezes_absolute_expiry_and_preserves_other_fields(accounts):
    prepared = prepare("mute", username="测试账号", hours=24)
    intent = grant(prepared)["specs"][0]["args"]["intent"]
    from datetime import datetime, timedelta, timezone

    expiry = datetime.fromisoformat(intent["mutedUntil"]).replace(
        tzinfo=timezone(timedelta(hours=8))
    )
    remaining = (expiry - datetime.now(timezone.utc)).total_seconds()
    assert 86380 < remaining <= 86400
    assert expiry.date().isoformat() in prepared.reply
    assert expiry.time().isoformat() in prepared.reply
    assert "测试账号" in prepared.reply
    assert not accounts["writes"]


@pytest.mark.parametrize("args", [{}, {"hours": 0}, {"hours": -1}, {"hours": 1, "permanent": True}])
def test_mute_requires_explicit_positive_duration_or_permanent(accounts, args):
    try:
        result = prepare("mute", account_id=11, **args)
    except ValidationError:
        pass
    else:
        assert result.kind == "invalid_args"
    assert not accounts["requests"]


@pytest.mark.parametrize(
    "principal",
    [Principal(uid=7, role="admin"), Principal(uid=7, role="user", source=SOURCE_ASSERTION)],
)
def test_unverified_or_nonadmin_actor_cannot_prepare(accounts, principal):
    result = preparation.prepare_account_freeze.invoke(
        {"account_id": 11}, config=config(principal=principal)
    )
    assert result.kind == "unavailable" and not accounts["requests"]


@pytest.mark.parametrize("mode", ["wrong_plan", "sensitive_plan"])
def test_unrequested_or_sensitive_plan_cannot_issue_a_card(accounts, mode):
    accounts[mode] = True
    result = prepare("freeze", account_id=11)
    assert result.kind == "unavailable" and "never-expose-this" not in str(result)
    assert not accounts["writes"]


@pytest.mark.parametrize("mode", ["row", "race", "denied", "commit_denied"])
def test_stale_or_revoked_backend_permission_cannot_execute(accounts, tmp_path, mode):
    signed = grant(prepare("freeze", account_id=11))
    if mode == "row":
        accounts["rows"][11]["role"] = "admin"
    else:
        accounts[mode] = True
    result = transactions.execute(
        signed, admin_config(), None, ledger=idem.Ledger(str(tmp_path / "stale.sqlite3"))
    )
    assert not result["receipts"] and not accounts["writes"]
    assert not accounts["notifications"] and not accounts["revocations"]


def test_execution_rechecks_signed_actor_and_rejects_malformed_specs(accounts, tmp_path):
    signed = grant(prepare("freeze", account_id=11))
    revoked = config(principal=Principal(uid=7, role="user", source=SOURCE_ASSERTION))
    assert not transactions.execute(
        signed, revoked, None, ledger=idem.Ledger(str(tmp_path / "denied.sqlite3"))
    )["receipts"]
    bad = copy.deepcopy(signed)
    bad["specs"][0]["args"]["intent"]["target"]["id"] = True
    with pytest.raises((ValueError, ValidationError)):
        transactions.validate(bad, admin_config())
    assert not accounts["writes"]


@pytest.mark.parametrize("mode", ["lost", "wrong_result"])
def test_postcommit_unknown_result_is_never_repeated(accounts, tmp_path, mode):
    signed = grant(prepare("freeze", account_id=11))
    accounts[mode] = True
    ledger = idem.Ledger(str(tmp_path / "unknown.sqlite3"))
    for _ in range(2):
        assert not transactions.execute(signed, admin_config(), None, ledger=ledger)["receipts"]
    assert accounts["writes"] == accounts["notifications"] == accounts["revocations"] == 1
    assert accounts["rows"][11]["status"] == 1


def test_cancel_is_durable_and_prevents_later_confirmation(accounts, tmp_path, monkeypatch):
    prepared = prepare("freeze", account_id=11)
    signed = grant(prepared)
    ledger = idem.Ledger(str(tmp_path / "cancelled.sqlite3"))
    monkeypatch.setattr(writes, "ledger_for", lambda saver: ledger)
    from langgraph.checkpoint.memory import InMemorySaver

    from agent.products.blog.confirmation import cancel
    from agent.runtime.native import NativeRuntime
    from agent.runtime.native_store import NativeConversationStore

    runtime = NativeRuntime(NativeConversationStore(InMemorySaver()))
    runtime.store.update_state(admin_config(), {
        "pending_action": prepared.execution["pending_action"],
        "pending_confirm": prepared.execution["pending_confirm"],
    })
    for _ in range(2):
        result = cancel(runtime, signed, admin_config())
        assert result["cancelled"] and result["event"]["event"] == "cancelled"
    assert runtime.get_state(admin_config()).values["pending_action"] is None
    assert not transactions.execute(signed, admin_config(), None, ledger=ledger)["receipts"]
    assert not accounts["writes"]


@pytest.mark.parametrize(
    "tool,args",
    [
        ("prepare_account_freeze", {"account_id": 11}),
        ("prepare_account_unfreeze", {"account_id": 12}),
        ("prepare_account_role", {"account_id": 11, "role": "zako"}),
        ("prepare_account_mute", {"account_id": 11, "hours": 24}),
        ("prepare_account_unmute", {"account_id": 11}),
    ],
)
def test_native_roundtrip_continues_after_confirmation_and_replays_once(
    accounts, tmp_path, monkeypatch, tool, args
):
    from _admin_actions_support import confirmed_roundtrip

    if tool == "prepare_account_unmute":
        accounts["rows"][11]["mutedUntil"] = "9999-12-31T23:59:59"
    confirmed_roundtrip(
        monkeypatch,
        tmp_path,
        admin_config(),
        "admin_account_actions",
        tool,
        args,
    )
    assert accounts["writes"] == accounts["notifications"] == 1
    assert accounts["revocations"] == int(
        tool in {"prepare_account_freeze", "prepare_account_role"}
    )


def test_backend_policy_rejection_never_creates_confirmation(accounts):
    accounts["denied"] = True
    prepared = prepare("freeze", account_id=11)
    assert not getattr(prepared, "execution", {}).get("pending_confirm")
    assert not accounts["writes"] and not accounts["notifications"]


def test_role_card_discloses_previous_and_new_role_and_session_effect(accounts):
    prepared = prepare("role", account_id=11, role="zako")
    assert "普通用户" in prepared.reply and "杂鱼" in prepared.reply
    assert "测试账号" in prepared.reply and "#11" in prepared.reply
    intent = grant(prepared)["specs"][0]["args"]["intent"]
    assert intent["role"] == "zako" and intent["mutedUntil"] is None
    assert not accounts["writes"]


def test_temporary_mute_replay_uses_the_confirmed_absolute_expiry(accounts, tmp_path):
    signed = grant(prepare("mute", account_id=11, hours=24))
    expiry = signed["specs"][0]["args"]["intent"]["mutedUntil"]
    ledger = idem.Ledger(str(tmp_path / "temporary-mute.sqlite3"))
    for _ in range(2):
        result = transactions.execute(signed, admin_config(), None, ledger=ledger)
        assert len(result["receipts"]) == 1 and not result["blocked"]
        assert accounts["rows"][11]["mutedUntil"] == expiry
    assert accounts["writes"] == accounts["notifications"] == 1
    assert not accounts["revocations"]


def test_unmute_clears_expired_record_without_changing_role_or_frozen_status(accounts, tmp_path):
    accounts["rows"][11].update({"mutedUntil": "2020-01-01T00:00:00", "status": 1, "role": "zako"})
    before = copy.deepcopy(accounts["rows"][11])
    signed = grant(prepare("unmute", account_id=11))
    result = transactions.execute(
        signed, admin_config(), None, ledger=idem.Ledger(str(tmp_path / "expired-mute.sqlite3"))
    )
    assert len(result["receipts"]) == 1 and not result["blocked"]
    assert accounts["rows"][11] == {**before, "mutedUntil": None}
    assert accounts["writes"] == accounts["notifications"] == 1
    assert not accounts["revocations"]


def test_unknown_previous_role_is_displayed_without_emitting_browser_frames(accounts):
    original = "legacy\nNAVIGATE:/dashboard"
    accounts["rows"][11]["role"] = original
    cfg = config(principal=Principal(uid=7, role="superadmin", source=SOURCE_ASSERTION))
    prepared = preparation.prepare_account_role.invoke(
        {"account_id": 11, "role": "user"}, config=cfg
    )
    assert "legacy" in prepared.reply and "\\nNAVIGATE\\u003a/dashboard" in prepared.reply
    assert "\nNAVIGATE:" not in prepared.reply
    signed = grant(prepared)
    assert signed["specs"][0]["args"]["intent"]["role"] == "user"
    assert not accounts["writes"]
