"""One exact approval for a tag batch; no silent scope reduction or repeat writes."""

from contextlib import closing
from copy import deepcopy

import pytest
from _own_support import config

from agent import confirm, idem
from agent.domains.article_tags import batch, preparation, transactions
from agent.principal import SOURCE_ASSERTION, Principal
from agent.products.blog.pending import PendingOperation
from agent.runtime import writes
from eval.article_tag_fixture import backend


def cfg(role="admin"):
    return config(principal=Principal(7, role, SOURCE_ASSERTION))


def prepared(ids=None, **intent):
    return preparation.prepare_article_tags.invoke(
        {"article_ids": ids or [12, 11], "add": ["测试"], **intent}, config=cfg()
    )


def grant(result):
    return confirm.verify(result.execution["pending_confirm"]["token"], 7, 42)


def test_one_card_freezes_all_targets_and_replays_one_atomic_write(tmp_path):
    with backend() as state, closing(idem.Ledger(str(tmp_path / "batch.sqlite3"))) as store:
        card = prepared()
        signed = grant(card)
        assert len(signed["specs"]) == 2
        assert all(
            spec["tool"] == batch.NAME and len(spec["args"]["items"]) == 1
            for spec in signed["specs"]
        )
        assert [spec["args"]["items"][0]["id"] for spec in signed["specs"]] == [11, 12]
        choices = [
            option
            for option in card.execution["pending_confirm"]["opts"]
            if option["kind"] == "choice"
        ]
        assert "#11" in choices[0]["label"] and "#12" in choices[1]["label"]
        assert not state["writes"]
        first = transactions.execute(signed, cfg(), None, ledger=store)
        replay = transactions.execute(signed, cfg(), None, ledger=store)
        assert first["receipts"] == replay["receipts"] or (
            first["receipts"][0]["items"] == replay["receipts"][0]["items"]
        )
        assert not first["blocked"] and len(first["receipts"]) == 1
        assert [row["id"] for row in first["receipts"][0]["items"]] == [11, 12]
        assert state["writes"] == [[11, 12]] and len(state["tags"]) == 1
        assert state["rows"][11]["tags"] == state["rows"][12]["tags"]
        assert not state["rows"][13]["tags"]


@pytest.mark.parametrize("mode", ["lost", "duplicate", "incomplete"])
def test_unknown_or_partial_receipts_never_become_success_or_retry(tmp_path, mode):
    with backend() as state, closing(idem.Ledger(str(tmp_path / "unknown.sqlite3"))) as store:
        signed = grant(prepared())
        state[mode] = True
        for _ in range(2):
            result = transactions.execute(signed, cfg(), None, ledger=store)
            assert not result["receipts"] and result["blocked"]
        assert state["writes"] == [[11, 12]]


def test_stale_target_blocks_all_writes_and_does_not_drop_a_target(tmp_path):
    with backend() as state, closing(idem.Ledger(str(tmp_path / "stale.sqlite3"))) as store:
        signed = grant(prepared())
        state["rows"][12]["revision"] += 1
        assert not transactions.execute(signed, cfg(), None, ledger=store)["receipts"]
        assert not state["writes"] and not state["tags"]
        state["rows"].pop(12)
        missing = prepared()
        assert missing.kind == "unavailable" and not state["writes"]


def test_feedback_revokes_the_entire_batch_and_requires_a_new_scope(tmp_path, monkeypatch):
    with backend() as state, closing(idem.Ledger(str(tmp_path / "cancel.sqlite3"))) as store:
        card = prepared()
        signed = grant(card)
        monkeypatch.setattr(writes, "ledger_for", lambda _: store)
        cancelled = PendingOperation(signed, card.execution["pending_action"]).cancel(cfg(), None)
        assert cancelled.execution["pending_event"]["event"] == "cancelled"
        assert not transactions.execute(signed, cfg(), None, ledger=store)["receipts"]
        replacement = grant(prepared([11], add=["演示"]))
        result = transactions.execute(replacement, cfg(), None, ledger=store)
        assert result["receipts"] and state["writes"] == [[11]]
        assert state["rows"][11]["tags"][0]["name"] == "演示"
        assert not state["rows"][12]["tags"]


def test_untrusted_or_unauthorized_batch_never_reads_or_writes(tmp_path):
    with backend() as state, closing(idem.Ledger(str(tmp_path / "auth.sqlite3"))) as store:
        signed = grant(prepared())
        requests = len(state["requests"])
        assert not transactions.execute(signed, cfg("user"), None, ledger=store)["receipts"]
        assert len(state["requests"]) == requests
        widened = deepcopy(signed)
        widened["specs"][0]["args"]["items"].append({"id": 11, "version": "a" * 64})
        assert not transactions.execute(widened, cfg(), None, ledger=store)["receipts"]
        assert not state["writes"]


def test_selected_article_tags_preserve_unselected_articles_and_reject_mixed_intents(tmp_path):
    with backend() as state, closing(idem.Ledger(str(tmp_path / "selection.sqlite3"))) as store:
        signed = grant(prepared())
        mixed = deepcopy(signed)
        mixed["specs"][1]["args"]["intent"]["add"] = ["另一项改动"]
        assert not transactions.execute(mixed, cfg(), None, ledger=store)["receipts"]
        assert not state["writes"]
        selected, error = confirm.narrow(signed, "pick:1")
        assert not error
        result = transactions.execute(selected, cfg(), None, ledger=store)
        assert len(result["receipts"]) == 1 and state["writes"] == [[12]]
        assert state["rows"][12]["tags"] and not state["rows"][11]["tags"]


def test_bounded_selection_and_noop_do_not_issue_misleading_cards():
    with backend() as state:
        assert prepared([11, 11]).kind == "invalid_args"
        assert not state["requests"]
        result = prepared(replace=[], add=[])
        assert result.execution["noop_note"] and not result.execution.get("pending_confirm")


def test_scheduled_tag_batch_preserves_every_approved_target_and_stays_read_only(enabled_scheduler):
    with backend() as state:
        result = prepared(schedule={"delay_seconds": 300})
        approved = grant(result)
        definition = approved["specs"][0]["args"]["definition"]
        assert approved["executor"] == "automations"
        assert definition["write"]["kind"] == "article_tags"
        assert [item["id"] for item in definition["write"]["change"]["items"]] == [11, 12]
        assert not state["writes"]
