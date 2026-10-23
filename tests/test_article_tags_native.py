"""Article tags use a frozen atomic plan and native approval lifecycle."""

import copy
import hashlib
import json
import secrets

import httpx
import pytest
from _own_support import config
from pydantic import ValidationError

from agent import confirm, idem
from agent.domains.article_tags import preparation, transactions
from agent.principal import SOURCE_ASSERTION, Principal
from agent.runtime import writes
from agent.runtime.pending import PendingOperation
from tools import base


def admin_config(**values):
    return config(principal=Principal(7, "admin", SOURCE_ASSERTION), **values)


@pytest.fixture
def tags(monkeypatch):
    state = {"tags": [], "revision": 0, "writes": 0, "requests": []}

    def plan(intent):
        before = copy.deepcopy(state["tags"])
        after = [] if intent["replace"] is not None else copy.deepcopy(before)
        after = [tag for tag in after if tag["name"] not in intent["remove"]]
        for name in intent["replace"] if intent["replace"] is not None else intent["add"]:
            if not any(tag["name"] == name for tag in after):
                after.append(
                    {
                        "id": None,
                        "level": 1,
                        "name": name,
                        "parentId": None,
                        "color": intent["color"],
                    }
                )
        version = hashlib.sha256(
            json.dumps([intent, before, after, state["revision"]], sort_keys=True).encode()
        ).hexdigest()
        return {"id": 11, "title": "Article", "version": version, "before": before, "after": after}

    def handle(request):
        state["requests"].append(request)
        body = json.loads(request.content)
        if request.url.path.endswith("/prepare"):
            if state.get("unavailable"):
                return httpx.Response(503)
            return httpx.Response(200, json={"code": 200, "data": plan(body)})
        state["writes"] += 1
        if state.get("lost"):
            raise httpx.ReadTimeout("response lost")
        actual = plan(body["intent"])
        assert body["version"] == actual["version"]
        if not state.get("ignore"):
            for index, tag in enumerate(actual["after"]):
                if tag["id"] is None:
                    tag["id"] = 100 + index
            state["tags"] = copy.deepcopy(actual["after"])
        else:
            actual["after"] = actual["before"]
        return httpx.Response(
            200,
            json={
                "code": 200,
                "data": {"beforeVersion": body["version"], "changed": True, "state": actual},
            },
        )

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        monkeypatch.setattr(base, "_client", client)
        monkeypatch.setattr(confirm.settings, "jwt_secret", secrets.token_urlsafe(32))
        yield state


def prepare(**args):
    return preparation.prepare_article_tags.invoke(
        {"article_id": 11, **args}, config=admin_config()
    )


def grant(result):
    return confirm.verify(result.execution["pending_confirm"]["token"], 7, 42)


def test_prepare_exact_name_is_read_only_and_confirmed_replay_is_one_write(tags, tmp_path):
    prepared = prepare(add=["测试"])
    signed = grant(prepared)
    assert signed["specs"][0]["args"]["intent"]["add"] == ["测试"]
    assert signed["executor"] == "article_tags" and not tags["writes"]
    assert "新建一级" in prepared.reply and "测试当标签" not in prepared.reply
    store = idem.Ledger(str(tmp_path / "replay.sqlite3"))
    for _ in range(2):
        outcome = transactions.execute(signed, admin_config(), None, ledger=store)
        assert len(outcome["receipts"]) == 1 and not outcome["blocked"]
    assert tags["writes"] == 1 and tags["tags"][0]["name"] == "测试"


@pytest.mark.parametrize("mode", ["lost", "ignore"])
def test_unknown_or_mismatched_result_is_never_repeated(tags, tmp_path, mode):
    signed = grant(prepare(add=["测试"]))
    tags[mode] = True
    store = idem.Ledger(str(tmp_path / "unknown.sqlite3"))
    for _ in range(2):
        assert not transactions.execute(signed, admin_config(), None, ledger=store)["receipts"]
    assert tags["writes"] == 1


def test_stale_plan_and_revoked_plan_are_zero_write(tags, tmp_path, monkeypatch):
    prepared = prepare(add=["测试"])
    signed = grant(prepared)
    tags["revision"] += 1
    store = idem.Ledger(str(tmp_path / "stale.sqlite3"))
    assert not transactions.execute(signed, admin_config(), None, ledger=store)["receipts"]
    assert not tags["writes"]
    prepared = prepare(add=["演示"])
    signed = grant(prepared)
    monkeypatch.setattr(writes, "ledger_for", lambda saver: store)
    pending = PendingOperation(signed, prepared.execution["pending_action"])
    assert pending.cancel(admin_config(), None).execution["pending_event"]["event"] == "cancelled"
    assert not transactions.execute(signed, admin_config(), None, ledger=store)["receipts"]
    assert not tags["writes"]


def test_noop_does_not_issue_a_card(tags):
    prepared = prepare(replace=[])
    assert prepared.execution["noop_note"] and not prepared.execution.get("pending_confirm")
    assert not tags["writes"]


@pytest.mark.parametrize("role,source", [("user", SOURCE_ASSERTION), ("admin", "body")])
def test_unauthorized_preparation_never_queries(tags, role, source):
    result = preparation.prepare_article_tags.invoke(
        {"article_id": 11, "add": ["测试"]}, config=config(principal=Principal(7, role, source))
    )
    assert result.kind == "unavailable" and not tags["requests"]


@pytest.mark.parametrize(
    "args",
    [
        {"article_id": True},
        {"article_id": "11"},
        {"add": [12]},
        {"level": True},
        {"replace": "测试"},
        {"add": ["x"] * 11},
    ],
)
def test_invalid_schema_stops_before_http(tags, args):
    with pytest.raises(ValidationError):
        preparation.prepare_article_tags.invoke({"article_id": 11, **args}, config=admin_config())
    assert not tags["requests"]


@pytest.mark.parametrize(
    "args",
    [
        {},
        {"add": ["x"], "replace": []},
        {"add": ["x"], "remove": ["x"]},
        {"add": [" "]},
        {"add": ["x"], "color": "red"},
    ],
)
def test_invalid_intent_is_no_http(tags, args):
    assert prepare(**args).kind == "invalid_args"
    assert not tags["requests"]


def test_native_runtime_preparation_confirmation_and_replay(tags, monkeypatch, tmp_path):
    from _admin_actions_support import confirmed_roundtrip

    confirmed_roundtrip(
        monkeypatch,
        tmp_path,
        admin_config(),
        "admin_catalog",
        "prepare_article_tags",
        {"article_id": 11, "add": ["测试"]},
    )
    assert tags["writes"] == 1


def test_worst_case_confirmation_stays_within_transport_budget(tags):
    names = ["测" * 39 + str(index) for index in range(10)]
    result = prepare(add=names)
    token = result.execution["pending_confirm"]["token"]
    assert len(token) < 4000 and grant(result) is not None
