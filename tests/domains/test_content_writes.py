"""Administrator content approvals bind exact effects, scope, identity and replay behavior."""

from contextlib import closing

import pytest
from _own_support import config

from agent import confirm, idem
from agent.domains import talks
from agent.domains.article_actions import deletion
from agent.domains.gallery import folders
from agent.principal import Principal
from eval.content_fixture import backend


def cfg(role="admin", source="assertion"):
    return config(principal=Principal(7, role, source))


def approved(result):
    grant = confirm.verify(result.execution["pending_confirm"]["token"], 7, 42)
    assert grant
    assert confirm.verify(result.execution["pending_confirm"]["token"], 8, 42) is None
    assert confirm.verify(result.execution["pending_confirm"]["token"], 7, 43) is None
    return grant


@pytest.mark.parametrize(
    "domain,arguments",
    [
        (talks, {"action": "create", "title": "新标题", "content": " 原文\n😀 "}),
        (talks, {"action": "update", "talk_id": 11, "content": "改写后的正文"}),
        (talks, {"action": "delete", "talk_id": 11}),
        (folders, {"action": "create", "name": "封面", "parent_id": 21}),
        (folders, {"action": "delete", "folder_id": 22}),
        (deletion, {"article_ids": [32, 31]}),
    ],
)
def test_native_approval_is_readonly_and_replay_performs_one_verified_write(
    tmp_path, domain, arguments
):
    tool = (
        talks.prepare_talk_change
        if domain is talks
        else folders.prepare_gallery_folder
        if domain is folders
        else deletion.prepare_article_deletions
    )
    with backend() as state, closing(idem.Ledger(str(tmp_path / "ledger.db"))) as ledger:
        grant = approved(tool.invoke(arguments, config=cfg()))
        assert state["writes"] == 0
        result = domain.execute(grant, cfg(), None, ledger=ledger)
        assert result["receipts"] and not result["blocked"]
        assert state["writes"] == 1
        replay = domain.execute(grant, cfg(), None, ledger=ledger)
        assert replay["receipts"] and state["writes"] == 1
        if domain is talks and arguments["action"] == "create":
            assert state["posts"][12]["content"] == " 原文\n😀 "
            assert state["posts"][12]["authorId"] == 7
        if domain is folders and arguments["action"] == "create":
            assert state["folders"][23]["parentId"] == 21
        if domain is deletion:
            assert state["articles"] == {}


@pytest.mark.parametrize(
    "domain,tool,arguments",
    [
        (talks, talks.prepare_talk_change, {"action": "create", "content": "正文"}),
        (folders, folders.prepare_gallery_folder, {"action": "create", "name": "封面"}),
        (deletion, deletion.prepare_article_deletions, {"article_ids": [31, 32]}),
    ],
)
@pytest.mark.parametrize("failure", ["lost", "tamper"])
def test_unknown_or_forged_receipt_cannot_be_success_or_trigger_a_second_write(
    tmp_path, domain, tool, arguments, failure
):
    with backend() as state, closing(idem.Ledger(str(tmp_path / "ledger.db"))) as ledger:
        grant = approved(tool.invoke(arguments, config=cfg()))
        state[failure] = True
        result = domain.execute(grant, cfg(), None, ledger=ledger)
        assert not result["receipts"] and result["blocked"]
        assert state["writes"] == 1
        domain.execute(grant, cfg(), None, ledger=ledger)
        assert state["writes"] == 1


@pytest.mark.parametrize(
    "role,source", [("user", "assertion"), ("admin", "body"), ("zako", "assertion")]
)
def test_nonadmin_or_unverified_actors_cannot_even_prepare_content_mutations(role, source):
    with backend() as state:
        for tool, args in [
            (talks.prepare_talk_change, {"action": "create", "content": "正文"}),
            (folders.prepare_gallery_folder, {"action": "create", "name": "封面"}),
            (deletion.prepare_article_deletions, {"article_ids": [31]}),
        ]:
            assert tool.invoke(args, config=cfg(role, source)).kind == "unavailable"
        assert not state["requests"]


def test_guestbook_ids_and_nonempty_folder_are_not_mutable_through_other_domains():
    with backend() as state:
        assert (
            talks.prepare_talk_change.invoke({"action": "delete", "talk_id": 99}, config=cfg()).kind
            != "ok"
        )
        assert (
            folders.prepare_gallery_folder.invoke(
                {"action": "delete", "folder_id": 21}, config=cfg()
            ).kind
            != "ok"
        )
        assert not state["writes"]


def test_selected_deletion_never_expands_to_unselected_articles(tmp_path):
    with backend() as state, closing(idem.Ledger(str(tmp_path / "ledger.db"))) as ledger:
        grant = approved(
            deletion.prepare_article_deletions.invoke({"article_ids": [31, 32]}, config=cfg())
        )
        narrowed, error = confirm.narrow(grant, "pick:0")
        assert narrowed and not error
        result = deletion.execute(narrowed, cfg(), None, ledger=ledger)
        assert result["receipts"] and set(state["articles"]) == {32}


def test_changed_document_blocks_the_whole_deletion_batch(tmp_path):
    with backend() as state, closing(idem.Ledger(str(tmp_path / "ledger.db"))) as ledger:
        grant = approved(
            deletion.prepare_article_deletions.invoke({"article_ids": [31, 32]}, config=cfg())
        )
        state["articles"][32]["version"] = "d" * 64
        result = deletion.execute(grant, cfg(), None, ledger=ledger)
        assert result["blocked"] and not result["receipts"]
        assert set(state["articles"]) == {31, 32} and state["writes"] == 0


@pytest.mark.parametrize(
    "domain,tool,args,kind",
    [
        (talks, talks.prepare_talk_change, {"action": "create", "content": "正文"}, "talks"),
        (
            folders,
            folders.prepare_gallery_folder,
            {"action": "create", "name": "封面"},
            "gallery_folders",
        ),
        (
            deletion,
            deletion.prepare_article_deletions,
            {"article_ids": [31, 32]},
            "article_deletions",
        ),
    ],
)
def test_scheduled_content_uses_one_frozen_transaction_with_no_premature_write(
    domain, tool, args, kind, enabled_scheduler
):
    with backend() as state:
        grant = approved(tool.invoke({**args, "schedule": {"delay_seconds": 300}}, config=cfg()))
        assert grant["executor"] == "automations"
        definition = grant["specs"][0]["args"]["definition"]
        assert definition["action"] == "admin_write" and definition["delay_seconds"] == 300
        assert definition["write"]["kind"] == kind and state["writes"] == 0


def test_unchanged_post_needs_no_approval_but_a_future_change_still_does(enabled_scheduler):
    with backend() as state:
        result = talks.prepare_talk_change.invoke(
            {"action": "update", "talk_id": 11, "content": "原正文"}, config=cfg()
        )
        assert result.execution["task_satisfied"]
        assert not result.execution.get("pending_confirm")
        grant = approved(
            talks.prepare_talk_change.invoke(
                {
                    "action": "update",
                    "talk_id": 11,
                    "content": "原正文",
                    "schedule": {"delay_seconds": 300},
                },
                config=cfg(),
            )
        )
        assert grant["executor"] == "automations" and state["writes"] == 0
