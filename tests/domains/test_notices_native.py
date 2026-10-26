"""Notice approval binds the recipient and exact text to one durable send."""

import copy
import hashlib
import json

import httpx
import pytest
from _own_support import config

from agent import confirm, idem
from agent.domains.notices import preparation, targets, transactions
from agent.principal import SOURCE_ASSERTION, SOURCE_BODY, Principal
from agent.products.blog.pending import PendingOperation
from agent.runtime import writes
from tools import base


def admin_config(**values):
    return config(principal=Principal(uid=7, role="admin", source=SOURCE_ASSERTION), **values)


@pytest.fixture
def notices(monkeypatch):
    state = {
        "recipient": {"id": 11, "username": "测试账号", "role": "user"},
        "revision": 0,
        "writes": 0,
        "requests": [],
        "saved": [],
    }

    def plan(i):
        if any(v is not None and state["recipient"][k] != v for k, v in i["target"].items()):
            return None
        title = (
            i["title"] if i["title"] is not None and i["title"].strip() else targets.DEFAULT_TITLE
        )
        version = hashlib.sha256(
            json.dumps([state["recipient"], i, state["revision"]], sort_keys=True).encode()
        ).hexdigest()
        return {
            "version": version,
            "recipient": copy.deepcopy(state["recipient"]),
            "title": title,
            "content": i["content"],
        }

    def handle(req):
        state["requests"].append(req)
        body = json.loads(req.content)
        if req.url.path.endswith("/prepare"):
            p = plan(body)
            if p is None:
                return httpx.Response(200, json={"code": 500, "message": "missing"})
            if state.get("wrong_plan"):
                p["content"] = "unrequested"
            return httpx.Response(200, json={"code": 200, "data": p})
        if state.get("race"):
            state["revision"] += 1
        p = plan(body["intent"])
        if p is None or p["version"] != body["version"]:
            return httpx.Response(200, json={"code": 409, "message": "stale"})
        state["writes"] += 1
        state["saved"].append(copy.deepcopy(p))
        if state.get("lost"):
            raise httpx.ReadTimeout("response lost after commit")
        result = {"notificationId": 101, "delivered": True, "state": p}
        if state.get("wrong_result"):
            result["state"]["recipient"]["id"] = 12
        return httpx.Response(200, json={"code": 200, "data": result})

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        monkeypatch.setattr(base, "_client", client)
        monkeypatch.setattr("config.settings.jwt_secret", "native-notice-test-secret-at-least-32")
        yield state


def prepare(**args):
    return preparation.prepare_user_notice.invoke(
        {"content": " 原文\n保持😀\t ", **args}, config=admin_config()
    )


def grant(prepared):
    return confirm.verify(prepared.execution["pending_confirm"]["token"], 7, 42)


@pytest.mark.parametrize("target", [{"user_id": 11}, {"username": "测试账号"},
                                     {"user_id": 11, "username": "测试账号"}])
def test_full_text_readonly_preparation_and_exactly_one_send(notices, tmp_path, target):
    prepared = prepare(**target)
    signed = grant(prepared)
    assert signed and notices["writes"] == 0 and len(notices["requests"]) == 1
    assert set(signed["specs"][0]["args"]) == {"version", "intent"}
    question = prepared.execution["pending_confirm"]["q"]
    assert "正文：\n\n 原文\n保持😀\t " in question
    store = idem.Ledger(str(tmp_path / "notice.sqlite3"))
    for _ in range(2):
        result = transactions.execute(signed, admin_config(), None, ledger=store)
        assert result["receipts"][0]["tool"] == "send_user_notice"
    assert notices["writes"] == 1
    assert notices["saved"][0]["title"] == targets.DEFAULT_TITLE
    assert notices["saved"][0]["content"] == " 原文\n保持😀\t "


@pytest.mark.parametrize("failure", ["stale", "race", "lost", "wrong_result"])
def test_changed_permissions_or_uncertain_delivery_never_repeats(notices, tmp_path, failure):
    signed = grant(prepare(user_id=11))
    if failure == "stale":
        notices["revision"] += 1
    else:
        notices[failure] = True
    store = idem.Ledger(str(tmp_path / "failed.sqlite3"))
    for _ in range(2):
        assert not transactions.execute(signed, admin_config(), None, ledger=store)["receipts"]
    assert notices["writes"] == int(failure in ("lost", "wrong_result"))


def test_revocation_is_durable_and_never_sends(notices, tmp_path, monkeypatch):
    prepared = prepare(user_id=11)
    signed = grant(prepared)
    store = idem.Ledger(str(tmp_path / "cancel.sqlite3"))
    monkeypatch.setattr(writes, "ledger_for", lambda _: store)
    pending = PendingOperation(signed, prepared.execution["pending_action"])
    for _ in range(2):
        assert (
            pending.cancel(admin_config(), None).execution["pending_event"]["event"] == "cancelled"
        )
    assert not transactions.execute(signed, admin_config(), None, ledger=store)["receipts"]
    assert not notices["writes"]


@pytest.mark.parametrize(
    "principal", [Principal(7, "user", SOURCE_ASSERTION), Principal(7, "admin", SOURCE_BODY)]
)
def test_unverified_or_unprivileged_actors_do_no_io(notices, principal):
    result = preparation.prepare_user_notice.invoke(
        {"content": "x", "user_id": 11}, config=config(principal=principal)
    )
    assert isinstance(result, base.ToolResult) and not notices["requests"]


@pytest.mark.parametrize(
    "args",
    [
        {},
        {"user_id": 11, "username": " "},
        {"user_id": 11, "content": " "},
        {"user_id": 11, "content": "\0"},
    ],
)
def test_invalid_or_ambiguous_requests_do_no_io(notices, args):
    assert isinstance(prepare(**args), base.ToolResult)
    assert not notices["requests"]


def test_service_cannot_change_text_or_recipient(notices):
    notices["wrong_plan"] = True
    assert isinstance(prepare(user_id=11), base.ToolResult)
    assert not notices["writes"]


def test_id_and_username_must_identify_the_same_notice_recipient(notices):
    result = prepare(user_id=11, username="其他账号")
    assert isinstance(result, base.ToolResult) and len(notices["requests"]) == 1
    assert not notices["writes"]


def test_max_unicode_notice_is_signed_without_truncation(notices):
    prepared = prepare(user_id=11, content="😀" * 1000, title="文" * 128)
    signed = grant(prepared)
    assert signed["specs"][0]["args"]["intent"]["content"] == "😀" * 1000
    assert len(prepared.execution["pending_confirm"]["token"]) <= confirm.MAX_TOKEN_CHARS


def test_native_roundtrip_continues_after_confirmation_and_replays_once(
        notices, tmp_path, monkeypatch):
    from _admin_actions_support import confirmed_roundtrip

    confirmed_roundtrip(
        monkeypatch,
        tmp_path,
        admin_config(),
        "admin_notices",
        "prepare_user_notice",
        {"user_id": 11, "content": "问候"},
    )
    assert notices["writes"] == 1


def test_service_cannot_substitute_an_unlisted_recipient_role(notices):
    notices["recipient"]["role"] = "superadmin"
    assert isinstance(prepare(user_id=11), base.ToolResult)
    assert not notices["writes"]
