"""Article confirmations freeze exact targets and never repeat uncertain writes."""

import json
import secrets

import httpx
import pytest
from _own_support import config
from pydantic import ValidationError

from agent import confirm, idem
from agent.domains.article_actions import preparation, transactions
from agent.principal import SOURCE_ASSERTION, Principal
from agent.products.blog.pending import PendingOperation
from agent.runtime import writes
from tools import base


def admin_config(**values):
    return config(principal=Principal(7, "admin", SOURCE_ASSERTION), **values)


@pytest.fixture
def articles(monkeypatch):
    state = {
        "row": {
            "id": 11,
            "title": "Article",
            "status": "private",
            "isPublic": False,
            "isTop": 0,
            "version": "a" * 64,
        },
        "requests": [],
        "writes": 0,
    }

    def handle(request):
        state["requests"].append(request)
        assert request.headers.get("Authorization", "").startswith("Bearer ")
        if request.method == "GET":
            if state.get("unavailable") and state["writes"]:
                return httpx.Response(503)
            return httpx.Response(200, json={"code": 200, "data": state["row"]})
        data = json.loads(request.content)
        state["writes"] += 1
        if state.get("lost"):
            raise httpx.ReadTimeout("response lost after submission")
        assert set(data) == {"version", "status", "isTop"}
        if data["version"] != state["row"]["version"]:
            return httpx.Response(200, json={"code": 500, "data": {}})
        if not state.get("ignore"):
            state["row"] = dict(state["row"], version="b" * 64)
            if data["status"] is not None:
                state["row"].update(status=data["status"], isPublic=data["status"] == "public")
            if data["isTop"] is not None:
                state["row"]["isTop"] = data["isTop"]
        return httpx.Response(
            200,
            json={
                "code": 200,
                "data": {"changed": True, "beforeVersion": data["version"], "state": state["row"]},
            },
        )

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        monkeypatch.setattr(base, "_client", client)
        monkeypatch.setattr(confirm.settings, "jwt_secret", secrets.token_urlsafe(32))
        yield state


def prepare(**args):
    return preparation.prepare_article_state.invoke(
        {"article_id": 11, **args}, config=admin_config()
    )


def grant(result):
    return confirm.verify(result.execution["pending_confirm"]["token"], 7, 42)


@pytest.mark.parametrize(
    "args", [{"status": "public"}, {"is_top": 1}, {"status": "draft", "is_top": 1}]
)
def test_preparation_is_read_only_and_confirmation_replays_once(articles, tmp_path, args):
    result = prepare(**args)
    signed = grant(result)
    assert (
        signed["executor"] == "article_state" and signed["specs"][0]["args"]["version"] == "a" * 64
    )
    assert articles["writes"] == 0
    assert result.execution["pending_confirm"]["token"] not in result.reply
    ledger = idem.Ledger(str(tmp_path / "writes.sqlite3"))
    first = transactions.execute(signed, admin_config(), None, ledger=ledger)
    second = transactions.execute(signed, admin_config(), None, ledger=ledger)
    assert articles["writes"] == 1
    assert len(first["receipts"]) == len(second["receipts"]) == 1
    assert not first["blocked"] and first["reply"] == second["reply"]
    if "status" in args:
        assert articles["row"]["status"] == args["status"]
    if "is_top" in args:
        assert articles["row"]["isTop"] == args["is_top"]


@pytest.mark.parametrize("mode", ["lost", "unavailable", "ignore"])
def test_uncertain_or_unchanged_response_never_becomes_success(articles, tmp_path, mode):
    signed = grant(prepare(status="public"))
    articles[mode] = True
    ledger = idem.Ledger(str(tmp_path / "unknown.sqlite3"))
    first = transactions.execute(signed, admin_config(), None, ledger=ledger)
    second = transactions.execute(signed, admin_config(), None, ledger=ledger)
    assert articles["writes"] == 1 and not first["receipts"] and not second["receipts"]


def test_target_changed_after_confirmation_is_zero_write(articles, tmp_path):
    signed = grant(prepare(status="public"))
    articles["row"]["version"] = "c" * 64
    outcome = transactions.execute(
        signed, admin_config(), None, ledger=idem.Ledger(str(tmp_path / "stale.sqlite3"))
    )
    assert not outcome["receipts"] and articles["writes"] == 0


def test_noop_does_not_issue_a_card_or_write(articles):
    result = prepare(status="private", is_top=0)
    assert result.execution["noop_note"] and not result.execution.get("pending_confirm")
    assert articles["writes"] == 0


@pytest.mark.parametrize("role,source", [("user", SOURCE_ASSERTION), ("admin", "body")])
def test_unverified_or_non_admin_does_not_even_read(articles, role, source):
    result = preparation.prepare_article_state.invoke(
        {"article_id": 11, "status": "public"}, config=config(principal=Principal(7, role, source))
    )
    assert result.kind == "unavailable" and not articles["requests"]


@pytest.mark.parametrize(
    "args",
    [
        {"article_id": True},
        {"article_id": "11"},
        {"article_id": 11, "is_top": True},
        {"article_id": 11, "status": "published"},
    ],
)
def test_invalid_schema_rejects_before_http(articles, args):
    with pytest.raises(ValidationError):
        preparation.prepare_article_state.invoke(args, config=admin_config())
    assert not articles["requests"]


def test_no_fields_or_malformed_target_never_prepares(articles):
    assert isinstance(prepare(), base.ToolResult)
    articles["row"]["id"] = 12
    assert prepare(status="public").kind == "unavailable"


def test_revoked_confirmation_cannot_execute(articles, tmp_path, monkeypatch):
    prepared = prepare(status="public")
    signed = grant(prepared)
    ledger = idem.Ledger(str(tmp_path / "revoke.sqlite3"))
    monkeypatch.setattr(writes, "ledger_for", lambda saver: ledger)
    pending = PendingOperation(signed, prepared.execution["pending_action"])
    event = pending.cancel(admin_config(), None)
    assert event.execution["pending_event"]["event"] == "cancelled"
    outcome = transactions.execute(signed, admin_config(), None, ledger=ledger)
    assert not outcome["receipts"] and not articles["writes"]


def test_runtime_article_confirmation_and_replay_are_native(articles, monkeypatch, tmp_path):
    from _admin_actions_support import confirmed_roundtrip

    confirmed_roundtrip(
        monkeypatch,
        tmp_path,
        admin_config(),
        "admin_catalog",
        "prepare_article_state",
        {"article_id": 11, "status": "public"},
    )
    assert articles["writes"] == 1
