"""Quota changes bind concrete applications, exact accounts and durable operation IDs."""

import copy
import hashlib
import json
import secrets

import httpx
import pytest
from _own_support import config
from pydantic import ValidationError

from agent import confirm, idem
from agent.domains.quota_actions import preparation, transactions
from agent.principal import SOURCE_ASSERTION, Principal
from agent.runtime.results import TurnResult
from tools import base


def admin_config(**values):
    return config(**{"principal": Principal(7, "admin", SOURCE_ASSERTION), **values})


def stamp(state):
    value = copy.deepcopy(state)
    value.pop("version", None)
    if "request" in value:
        value.pop("used")
    state["version"] = hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()
    return state


@pytest.fixture
def quota(monkeypatch):
    account = {"userId": 12, "name": "reader", "role": "user", "used": 45, "limit": 500,
               "unlimited": False, "unit": "conversation_turns"}
    request = {"id": 31, "reason": "准备面试", "status": 0, "note": None,
               "createdAt": "2026-10-23 12:00:00.123456", "handledAt": None, "handledBy": None}
    state = {"account": account, "request": request, "requests": [], "writes": 0,
             "mode": None, "notices": 0}

    def snapshot(review):
        value = dict(state["account"])
        if review:
            value["request"] = dict(state["request"])
        return stamp(value)

    def handle(request):
        state["requests"].append(request)
        assert request.headers.get("Authorization", "").startswith("Bearer ")
        review = "/requests/" in request.url.path
        if request.method == "GET":
            if state["mode"] == "deleted" or (state["mode"] == "readback" and state["writes"]):
                return httpx.Response(503)
            assert request.url.path.endswith("/31" if review else "/12")
            value = snapshot(review)
            if state["mode"] == "wrong_user":
                value["userId"] = 13
            elif state["mode"] == "wrong_request" and review:
                value["request"]["id"] = 32
            elif state["mode"] == "missing_version":
                value.pop("version")
            return httpx.Response(200, json={"code": 200, "data": value})
        state["writes"] += 1
        payload = json.loads(request.content)
        before = snapshot(review)
        if state["mode"] == "race":
            return httpx.Response(200, json={"code": 500, "message": "stale target", "data": {}})
        assert payload["version"] == before["version"]
        if state["mode"] != "ignore":
            if review:
                state["request"].update(status=1 if payload["approved"] else 2,
                    note=None if payload["approved"] else payload["reason"],
                    handledAt="2026-10-23 12:01:00.123456", handledBy=7)
                if payload["approved"]:
                    state["account"]["used"] = 0
            else:
                state["account"]["used"] = 0
            state["notices"] += 1
        if state["mode"] == "timeout":
            raise httpx.ReadTimeout("sent; response lost", request=request)
        written = snapshot(review)
        if state["mode"] == "wrong_result":
            written["userId"] = 99
        if state["mode"] == "chat_after_reset":
            state["account"]["used"] += 1
        return httpx.Response(200, json={"code": 200, "data": {"state": written, "changed": True}})

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        monkeypatch.setattr(base, "_client", client)
        monkeypatch.setattr(confirm.settings, "jwt_secret", secrets.token_urlsafe(32))
        yield state


def prepare(action, **args):
    return preparation.prepare_quota_action.invoke({"action": action, **args},
                                                    config=admin_config())


def grant(result):
    return confirm.verify(result.execution["pending_confirm"]["token"], 7, 42)


@pytest.mark.parametrize("action,args,name", [
    ("approve", {"request_id": 31}, "approve_quota_request"),
    ("reject", {"request_id": 31, "reason": "本次申请暂不符合体验规则"}, "reject_quota_request"),
    ("reset", {"account_id": 12}, "reset_user_quota"),
])
def test_exact_targets_signed_then_written_once_and_replayed(quota, tmp_path, action, args, name):
    prepared = prepare(action, **args)
    assert isinstance(prepared, TurnResult) and quota["writes"] == 0
    signed = grant(prepared)
    assert signed["executor"] == "quota_actions"
    assert signed["specs"][0]["tool"] == name
    frozen = signed["specs"][0]["args"]
    assert frozen["user_id"] == 12 and frozen["name"] == "reader"
    assert len(frozen["version"]) == 64
    if action != "reset":
        assert frozen["request_id"] == 31
    if action == "reject":
        assert frozen["reason"] == args["reason"]
    ledger = idem.Ledger(str(tmp_path / "quota.sqlite3"))
    first = transactions.execute(signed, admin_config(), None, ledger=ledger)
    second = transactions.execute(signed, admin_config(), None, ledger=ledger)
    assert quota["writes"] == quota["notices"] == 1
    assert first["receipts"] and first["reply"] == second["reply"]
    assert first["receipts"][0]["args"] == frozen
    assert quota["account"]["used"] == (45 if action == "reject" else 0)


@pytest.mark.parametrize("account_name", ["agent_test_user_722", "agent_test_user_723"])
@pytest.mark.parametrize("action,args", [
    ("approve", {"request_id": 31}),
    ("reject", {"request_id": 31, "reason": "暂不符合条件"}),
    ("reset", {"account_id": 12}),
])
def test_confirmation_preserves_full_verified_names_with_the_same_prefix(
        quota, account_name, action, args):
    quota["account"]["name"] = account_name
    prepared = prepare(action, **args)
    question = prepared.execution["pending_confirm"]["q"]
    assert account_name in question
    assert "账号编号12" in question
    assert grant(prepared)["specs"][0]["args"]["name"] == account_name
    assert quota["writes"] == 0


@pytest.mark.parametrize("field,value", [("reason", "修改申请原因"),
    ("createdAt", "2026-10-23 13:00:00"), ("status", 2)])
def test_request_edits_revoke_frozen_target_before_any_write(quota, tmp_path, field, value):
    signed = grant(prepare("approve", request_id=31))
    quota["request"][field] = value
    result = transactions.execute(signed, admin_config(), None,
                                 ledger=idem.Ledger(str(tmp_path / "changed.sqlite3")))
    assert not result["receipts"] and quota["writes"] == 0


@pytest.mark.parametrize("field,value", [("name", "renamed"), ("role", "admin"), ("used", 46)])
def test_reset_identity_or_usage_change_requires_new_confirmation(quota, tmp_path, field, value):
    signed = grant(prepare("reset", account_id=12))
    quota["account"][field] = value
    result = transactions.execute(signed, admin_config(), None,
                                 ledger=idem.Ledger(str(tmp_path / "changed.sqlite3")))
    assert not result["receipts"] and quota["writes"] == 0


@pytest.mark.parametrize("mode", ["timeout", "ignore", "readback", "wrong_result", "race"])
def test_uncertain_outcomes_do_not_repeat_write_or_claim_success(quota, tmp_path, mode):
    signed = grant(prepare("approve", request_id=31))
    quota["mode"] = mode
    ledger = idem.Ledger(str(tmp_path / "uncertain.sqlite3"))
    for _ in range(2):
        result = transactions.execute(signed, admin_config(), None, ledger=ledger)
        assert not result["receipts"] and result["blocked"]
    assert quota["writes"] == 1


@pytest.mark.parametrize("mode", ["deleted", "wrong_request", "missing_version"])
def test_bad_or_deleted_request_never_prepares(quota, mode):
    quota["mode"] = mode
    result = prepare("approve", request_id=31)
    assert getattr(result, "kind", None) == "unavailable" and quota["writes"] == 0


@pytest.mark.parametrize("action,args", [("approve", {"account_id": 12}),
    ("approve", {"request_id": 31, "account_id": 12}),
    ("reject", {"request_id": 31}), ("reject", {"request_id": 31, "reason": " "}),
    ("reset", {"request_id": 31}), ("reset", {"account_id": 12, "reason": "extra"})])
def test_wrong_target_channels_or_unspecified_reason_never_read(quota, action, args):
    assert prepare(action, **args).kind == "unavailable"
    assert not quota["requests"]


@pytest.mark.parametrize("args", [{"action": "approve", "request_id": True},
    {"action": "reset", "account_id": "12"},
    {"action": "reject", "request_id": 31, "reason": "x" * 256}])
def test_schema_rejects_coercion_and_oversized_note(quota, args):
    with pytest.raises(ValidationError):
        preparation.prepare_quota_action.invoke(args, config=admin_config())
    assert not quota["requests"]


@pytest.mark.parametrize("unlimited", [False, True])
def test_zero_usage_and_unlimited_account_are_noops(quota, unlimited):
    quota["account"].update(used=0, limit=0 if unlimited else 500, unlimited=unlimited,
                            role="admin" if unlimited else "user")
    result = prepare("reset", account_id=12)
    assert isinstance(result, TurnResult) and not result.execution.get("pending_confirm")
    assert quota["writes"] == 0


def test_chat_after_reset_is_reported_as_current_usage_not_failed_reset(quota, tmp_path):
    signed = grant(prepare("reset", account_id=12))
    quota["mode"] = "chat_after_reset"
    result = transactions.execute(signed, admin_config(), None,
                                 ledger=idem.Ledger(str(tmp_path / "usage.sqlite3")))
    assert result["receipts"] and "当前读回已用1轮" in result["reply"]
    assert quota["writes"] == 1


@pytest.mark.parametrize("role,source", [("user", SOURCE_ASSERTION), ("admin", "body")])
def test_unverified_or_non_admin_never_reads_or_prepares(quota, role, source):
    result = preparation.prepare_quota_action.invoke({"action": "approve", "request_id": 31},
              config=config(principal=Principal(7, role, source)))
    assert result.kind == "unavailable" and not quota["requests"]


def test_approval_permits_natural_usage_growth_without_changing_target(quota, tmp_path):
    signed = grant(prepare("approve", request_id=31))
    quota["account"]["used"] += 1
    result = transactions.execute(signed, admin_config(), None,
                                 ledger=idem.Ledger(str(tmp_path / "approval.sqlite3")))
    assert result["receipts"] and quota["writes"] == 1
    assert quota["account"]["used"] == 0


@pytest.mark.parametrize("cancel", [True, False])
def test_cancel_or_changed_reason_replacement_revokes_old_decision(
    quota, monkeypatch, tmp_path, cancel
):
    from agent.domains import registry
    from agent.products.blog.pending import PendingOperation
    from agent.runtime import writes

    monkeypatch.setitem(registry._DOMAINS, "quota_actions", transactions)
    ledger = idem.Ledger(str(tmp_path / "revoke.sqlite3"))
    monkeypatch.setattr(writes, "ledger_for", lambda saver: ledger)
    initial = prepare("reject", request_id=31, reason="原来的理由")
    signed = grant(initial)
    pending = PendingOperation(signed, initial.execution["pending_action"])
    if cancel:
        result = pending.cancel_tool(None).invoke({}, config=admin_config())
        assert result.execution["pending_action"] is None
    else:
        replacement = prepare("reject", request_id=31, reason="用户更正后的新理由")
        result = pending.transition(replacement, admin_config(), None)
        replacement_grant = grant(result)
        assert replacement_grant["specs"][0]["args"]["reason"] == "用户更正后的新理由"
        assert replacement_grant["jti"] != signed["jti"]
    assert result.execution["pending_event"]["event"] == "cancelled"
    old = transactions.execute(signed, admin_config(), None, ledger=ledger)
    assert not old["receipts"] and quota["writes"] == 0


@pytest.mark.parametrize("change", [{"conversation_id": 43}, {"user_id": 8},
    {"principal": Principal(7, "user", SOURCE_ASSERTION)}])
def test_execution_binding_or_role_changes_never_send_write(quota, tmp_path, change):
    signed = grant(prepare("reset", account_id=12))
    before = len(quota["requests"])
    result = transactions.execute(signed, admin_config(**change), None,
                                 ledger=idem.Ledger(str(tmp_path / "identity.sqlite3")))
    assert not result["receipts"] and quota["writes"] == 0
    assert len(quota["requests"]) == before


@pytest.mark.parametrize("role,limit,unlimited", [("superadmin", 0, True), ("zako", 500, False)])
def test_existing_application_does_not_require_listable_account_role(
    quota, tmp_path, role, limit, unlimited
):
    quota["account"].update(role=role, limit=limit, unlimited=unlimited,
                            used=0 if unlimited else 45)
    signed = grant(prepare("approve", request_id=31))
    result = transactions.execute(signed, admin_config(), None,
                                 ledger=idem.Ledger(str(tmp_path / "role.sqlite3")))
    assert result["receipts"] and quota["writes"] == 1
    if unlimited:
        assert "不限额" in result["reply"] and "上限0轮" not in result["reply"]


@pytest.mark.parametrize("arguments", [
    {"action": "approve", "request_id": 31},
    {"action": "reject", "request_id": 31, "reason": "本次申请暂不符合体验规则"},
    {"action": "reset", "account_id": 12},
])
def test_real_runtime_prepares_confirms_and_replays_quota_once(
    quota, monkeypatch, tmp_path, arguments
):
    from _admin_actions_support import confirmed_roundtrip

    confirmed_roundtrip(monkeypatch, tmp_path, admin_config(), "admin_work_queue",
                        "prepare_quota_action", arguments)
    assert quota["writes"] == 1
