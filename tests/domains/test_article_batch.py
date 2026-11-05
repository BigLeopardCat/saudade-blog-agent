"""One batch approval freezes every target and replays one atomic request."""

import json
import secrets
from contextlib import closing
from copy import deepcopy

import httpx
import pytest
from _own_support import config

from agent import confirm, idem
from agent.domains.article_actions import batch, preparation, transactions
from agent.principal import SOURCE_ASSERTION, Principal
from tools import base


def cfg(role="admin"):
    return config(principal=Principal(7, role, SOURCE_ASSERTION))


@pytest.fixture
def service(monkeypatch):
    state = {
        "writes": 0,
        "requests": [],
        "rows": [
            {
                "id": ident,
                "title": f"Article {ident}",
                "status": "public" if ident < 13 else "private",
                "isPublic": ident < 13,
                "isTop": 0,
                "version": str(ident % 10) * 64,
            }
            for ident in (11, 12, 13)
        ],
    }

    def handle(request):
        state["requests"].append(request)
        assert request.headers["Authorization"].startswith("Bearer ")
        body = json.loads(request.content)
        if request.url.path.endswith("/prepare"):
            return httpx.Response(200, json={"code": 200, "data": state["rows"]})
        state["writes"] += 1
        if state.get("lost"):
            raise httpx.ReadTimeout("lost after submission")
        if state.get("stale"):
            return httpx.Response(200, json={"code": 500, "message": "changed"})
        rows = [
            {
                "changed": True,
                "beforeVersion": item["version"],
                "state": {
                    **next(row for row in state["rows"] if row["id"] == item["id"]),
                    "version": "f" * 64,
                    "status": body["status"],
                    "isPublic": body["status"] == "public",
                },
            }
            for item in body["items"]
        ]
        if state.get("duplicate"):
            rows[-1] = rows[0]
        return httpx.Response(200, json={"code": 200, "data": rows})

    monkeypatch.setattr(confirm.settings, "confirmation_secret", secrets.token_urlsafe(32))
    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        monkeypatch.setattr(base, "_client", client)
        yield state


def prepare():
    return preparation.prepare_article_state.invoke(
        {"category": "测试", "status": "private"}, config=cfg()
    )


def test_batch_has_one_card_and_one_idempotent_write(service, tmp_path):
    result = prepare()
    assert service["writes"] == 0
    card = result.execution["pending_confirm"]
    assert len(card["specs"]) == 2
    grant = confirm.verify(card["token"], 7, 42)
    assert [spec["args"]["items"][0]["id"] for spec in grant["specs"]] == [11, 12]
    assert all(len(spec["args"]["items"]) == 1 for spec in grant["specs"])
    choices = [option for option in card["opts"] if option["kind"] == "choice"]
    assert "#11" in choices[0]["label"] and "#12" in choices[1]["label"] and "1篇" in card["q"]
    ledger = idem.Ledger(str(tmp_path / "batch.sqlite3"))
    first = transactions.execute(grant, cfg(), None, ledger=ledger)
    replay = transactions.execute(grant, cfg(), None, ledger=ledger)
    assert len(first["receipts"]) == len(replay["receipts"]) == 1
    assert service["writes"] == 1
    assert not transactions.execute(grant, cfg("user"), None, ledger=ledger)["receipts"]
    assert service["writes"] == 1


def test_selected_article_state_executes_only_selected_ids_and_rejects_mixed_transactions(
    service, tmp_path
):
    card = prepare().execution["pending_confirm"]
    grant = confirm.verify(card["token"], 7, 42)
    with closing(idem.Ledger(str(tmp_path / "selection.sqlite3"))) as ledger:
        mixed = deepcopy(grant)
        mixed["specs"][1]["args"]["status"] = "draft"
        assert not transactions.execute(mixed, cfg(), None, ledger=ledger)["receipts"]
        assert service["writes"] == 0
        selected, error = confirm.narrow(grant, "pick:1")
        assert not error
        result = transactions.execute(selected, cfg(), None, ledger=ledger)
        assert len(result["receipts"]) == 1 and service["writes"] == 1
        payload = json.loads(service["requests"][-1].content)
        assert [item["id"] for item in payload["items"]] == [12]


@pytest.mark.parametrize("failure", ["lost", "stale", "duplicate"])
def test_unknown_or_incomplete_batch_results_never_claim_success_or_retry(
    service, tmp_path, failure
):
    grant = confirm.verify(prepare().execution["pending_confirm"]["token"], 7, 42)
    service[failure] = True
    ledger = idem.Ledger(str(tmp_path / "failed.sqlite3"))
    for _ in range(2):
        assert not transactions.execute(grant, cfg(), None, ledger=ledger)["receipts"]
    assert service["writes"] == 1


def test_noop_batch_and_invalid_identity_do_not_offer_an_approval(service):
    for row in service["rows"]:
        row.update(status="private", isPublic=False)
    assert prepare().execution["noop_note"] and service["writes"] == 0
    before = len(service["requests"])
    assert (
        preparation.prepare_article_state.invoke(
            {"category": "测试", "status": "private"}, config=cfg("user")
        ).kind
        == "unavailable"
    )
    assert len(service["requests"]) == before


@pytest.mark.parametrize(
    "items",
    [
        [],
        [{"id": True, "version": "a" * 64}],
        [{"id": 1, "version": "a" * 64}] * 2,
        [{"id": i + 1, "version": "a" * 64} for i in range(51)],
    ],
)
def test_batch_manifest_rejects_coerced_duplicate_and_unbounded_targets(items):
    with pytest.raises(ValueError):
        batch.validate({"items": items, "status": "private", "is_top": None})
