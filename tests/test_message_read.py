"""Received-message scopes, confirmation lifecycle and independent write evidence."""

import json

import pytest
from _own_support import config
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.memory import InMemorySaver
from pydantic import ValidationError

from agent import confirm, idem
from agent.domains.messages import preparation, transactions
from agent.runtime import writes
from agent.runtime.native import NativeRuntime
from agent.runtime.native_store import NativeConversationStore
from agent.runtime.results import TurnResult


def _prepare(scope="all_unread", ids=None, cfg=None):
    return preparation.prepare_message_read.invoke(
        {"scope": scope, "ids": ids or []}, config=cfg or config())


def _grant(result):
    return confirm.verify(result.execution["pending_confirm"]["token"], 7, 42)


def test_prepare_never_writes_and_excludes_outbox(mailbox):
    result = _prepare()
    assert _grant(result)["specs"] == [{"tool": "read_messages",
                                       "args": {"ids": [201, 202], "all": False}}]
    assert "欢迎来信" in result.reply and "发出的信" not in result.reply
    assert "不可撤销" in result.reply
    assert all(r.method == "GET" for r in mailbox["requests"])
    assert not isinstance(_prepare("selected", [301]), TurnResult)


@pytest.mark.parametrize("ids", [[True], ["201"], [0], [-1], list(range(1, 102))])
def test_invalid_ids_rejected_before_read(mailbox, ids):
    with pytest.raises(ValidationError):
        _prepare("selected", ids)
    assert not mailbox["requests"]


def test_incomplete_mailbox_cannot_prove_all_unread(mailbox):
    mailbox["items"] = [{"id": i, "toUserId": 7, "isRead": False} for i in range(1, 102)]
    assert not isinstance(_prepare(), TurnResult)
    assert isinstance(_prepare("selected", [1]), TurnResult)


@pytest.mark.parametrize("bad", [{"id": 201, "toUserId": 8, "isRead": False},
                                 {"id": 201, "toUserId": 7, "isRead": "false"},
                                 {"id": True, "toUserId": 7, "isRead": False}])
def test_malformed_or_foreign_received_rows_cannot_prepare(mailbox, bad):
    mailbox["items"] = [bad]
    assert not isinstance(_prepare("selected", [201]), TurnResult)


def test_unverified_identity_never_reads(mailbox):
    from agent.principal import Principal
    assert not isinstance(_prepare(cfg=config(principal=Principal(uid=7, role="user"))),
                          TurnResult)
    assert not mailbox["requests"]


def test_new_messages_and_outbox_are_unchanged_and_replay_writes_once(mailbox, tmp_path):
    grant = _grant(_prepare())
    mailbox["new_on_write"] = True
    store = idem.Ledger(str(tmp_path / "mail.sqlite3"))
    try:
        first = transactions.execute(grant, config(), None, ledger=store)
        replay = transactions.execute(grant, config(), None, ledger=store)
    finally:
        store.close()
    assert first["receipts"] and replay["receipts"]
    posts = [r for r in mailbox["requests"] if r.method == "POST"]
    assert len(posts) == 1
    assert json.loads(posts[0].content) == {"ids": [201, 202], "all": False}
    assert mailbox["items"][-1]["isRead"] is False
    assert mailbox["outbox"][0]["isRead"] is False


def test_partial_write_has_no_receipt_and_cannot_repeat(mailbox, tmp_path):
    grant = _grant(_prepare())
    mailbox["ignore"] = {202}
    store = idem.Ledger(str(tmp_path / "partial.sqlite3"))
    try:
        for _ in range(2):
            result = transactions.execute(grant, config(), None, ledger=store)
            assert result["receipts"] == [] and "结果未知" in result["reply"]
    finally:
        store.close()
    assert sum(r.method == "POST" for r in mailbox["requests"]) == 1


def test_replacing_and_cancelling_message_cards_revokes_old_grants(mailbox, monkeypatch, tmp_path):
    store = idem.Ledger(str(tmp_path / "cancel.sqlite3"))
    monkeypatch.setattr(writes, "ledger_for", lambda _: store)
    runtime = NativeRuntime(NativeConversationStore(InMemorySaver()))

    class Model:
        def __init__(self, domain, tool, args):
            self.responses = iter([
                AIMessage(content="", tool_calls=[{"name": f"load_{domain}_info_tools",
                                                    "args": {}, "id": "load"}]),
                AIMessage(content="", tool_calls=[{"name": tool, "args": args, "id": "call"}])])

        def bind_tools(self, *args, **kwargs):
            return self

        def invoke(self, prompt):
            return next(self.responses)

    def turn(domain, tool, args):
        model = Model(domain, tool, args)
        runtime.model_factory = lambda **_: model
        list(runtime.stream_events({"messages": [HumanMessage(content="synthetic request")]}, config()))

    try:
        turn("messages", "prepare_message_read", {"scope": "all_unread"})
        old = runtime.get_state(config()).values["pending_confirm"]
        turn("messages", "prepare_message_read", {"scope": "selected", "ids": [202]})
        new = runtime.get_state(config()).values["pending_confirm"]
        assert old != new
        list(runtime.stream_events(
            {"messages": [], "confirm_grant": confirm.verify(old["token"], 7, 42)}, config()))
        assert runtime.get_state(config()).values["pending_confirm"] == new
        turn("pending", "cancel_pending_operation", {})
        from agent.runtime.confirmation import confirmed_runtime
        selected = confirmed_runtime(runtime, confirm.verify(new["token"], 7, 42))
        list(selected.stream_events(
            {"messages": [], "confirm_grant": confirm.verify(new["token"], 7, 42)}, config()))
        assert runtime.get_state(config()).values["pending_action"] is None
        assert not any(r.method == "POST" for r in mailbox["requests"])
    finally:
        store.close()


def test_old_unread_messages_can_be_resolved_outside_the_recent_window(mailbox):
    mailbox["items"] = [{"id": i, "toUserId": 7, "isRead": i != 1}
                        for i in range(1, 103)]
    grant = _grant(_prepare())
    assert grant["specs"][0]["args"] == {"ids": [1], "all": False}
    assert mailbox["requests"][-1].url.params["unreadOnly"] == "true"
    assert _grant(_prepare("selected", [1]))["specs"][0]["args"]["ids"] == [1]
    assert mailbox["requests"][-1].url.params["ids"] == "1"
