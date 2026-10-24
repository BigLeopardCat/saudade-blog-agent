"""Fixed-scope notification confirmations and factual, replay-safe execution."""

import json

import pytest
from _own_support import config
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.memory import InMemorySaver
from pydantic import ValidationError

from agent import confirm, idem
from agent.domains.notifications import preparation, transactions
from agent.principal import Principal
from agent.runtime import writes
from agent.runtime.native import NativeRuntime
from agent.runtime.native_store import NativeConversationStore
from agent.runtime.results import TurnResult


def prepare(scope="all_unread", ids=None):
    return preparation.prepare_notification_read.invoke(
        {"scope": scope, "ids": ids or []}, config=config())


def grant(result):
    return confirm.verify(result.execution["pending_confirm"]["token"], 7, 42)


def test_prepare_all_pins_actual_unread_ids_and_never_writes(notifications):
    result = prepare()
    assert grant(result)["executor"] == "notifications"
    assert grant(result)["specs"] == [{"tool": "read_notifications",
                                      "args": {"ids": [101, 102], "all": False}}]
    assert "审核结果" in result.reply and "系统公告" in result.reply
    assert "不可撤销" in result.reply and "新到" in result.reply
    assert all(request.method == "GET" for request in notifications["requests"])


@pytest.mark.parametrize("ids", [[], [999]])
def test_empty_or_unknown_selection_cannot_prepare_a_card(notifications, ids):
    assert not isinstance(prepare("selected", ids), TurnResult)


@pytest.mark.parametrize("ids", [[True], ["101"], [-1], [0]])
def test_ids_are_strict_positive_integers(notifications, ids):
    with pytest.raises(ValidationError):
        prepare("selected", ids)
    assert not notifications["requests"]


def test_read_errors_and_truncated_all_selection_do_not_become_noops(notifications):
    notifications["unavailable"] = True
    assert not isinstance(prepare(), TurnResult)
    notifications["unavailable"] = False
    notifications["hidden_unread"] = 1
    assert not isinstance(prepare(), TurnResult)
    result = prepare("selected", [103])
    assert result.execution["noop_note"] and not result.execution.get("pending_confirm")


def test_unverified_or_unbound_identity_never_reads(notifications):
    for cfg in [config(principal=Principal(uid=7, role="user")),
                config(user_id=8), config(conversation_id=None)]:
        outcome = preparation.prepare_notification_read.invoke(
            {"scope": "all_unread"}, config=cfg)
        assert not isinstance(outcome, TurnResult)
    assert not notifications["requests"]


def test_confirmed_read_excludes_new_notifications_and_replays_once(notifications, tmp_path):
    payload = grant(prepare())
    notifications["items"].append({"id": 105, "title": "确认后新到", "isRead": False})
    notifications["new_on_write"] = True
    store = idem.Ledger(str(tmp_path / "notifications.sqlite3"))
    try:
        first = transactions.execute(payload, config(), None, ledger=store)
        second = transactions.execute(payload, config(), None, ledger=store)
    finally:
        store.close()
    assert first["receipts"] and second["receipts"]
    assert first["receipts"][0]["args"] == {"ids": [101, 102], "all": False}
    posts = [request for request in notifications["requests"] if request.method == "POST"]
    assert len(posts) == 1 and json.loads(posts[0].content) == {"ids": [101, 102], "all": False}
    assert all(not row["isRead"] for row in notifications["items"] if row["id"] in (104, 105))


def test_partial_application_never_produces_a_receipt_or_retries(notifications, tmp_path):
    payload = grant(prepare())
    notifications["ignore"] = {102}
    store = idem.Ledger(str(tmp_path / "partial.sqlite3"))
    try:
        first = transactions.execute(payload, config(), None, ledger=store)
        second = transactions.execute(payload, config(), None, ledger=store)
    finally:
        store.close()
    assert first["receipts"] == second["receipts"] == []
    assert "结果未知" in first["reply"]
    assert sum(request.method == "POST" for request in notifications["requests"]) == 1


@pytest.mark.parametrize("args", [{"ids": [101], "all": True},
                                  {"ids": [True], "all": False},
                                  {"ids": [102, 101], "all": False}])
def test_invalid_grants_never_open_the_ledger_or_write(notifications, args):
    payload = grant(prepare())
    payload["specs"][0]["args"] = args
    before = len(notifications["requests"])
    result = transactions.execute(payload, config(), None)
    assert result["blocked"] and not result["receipts"]
    assert len(notifications["requests"]) == before


class Model:
    def __init__(self, domain="notifications", name="prepare_notification_read", args=None):
        self.prompts = []
        self.responses = [AIMessage(content="", tool_calls=[{
            "name": f"load_{domain}_info_tools", "args": {}, "id": "load"}]),
            AIMessage(content="", tool_calls=[{
                "name": name, "args": {"scope": "all_unread"} if args is None else args,
                "id": "prepare"}])]

    def bind_tools(self, tools, **kwargs):
        return self

    def invoke(self, prompt):
        self.prompts.append(list(prompt))
        return self.responses.pop(0)


def test_native_pending_edit_cancel_and_stale_click_preserve_new_card(
        notifications, monkeypatch, tmp_path):
    store = idem.Ledger(str(tmp_path / "native.sqlite3"))
    monkeypatch.setattr(writes, "ledger_for", lambda saver: store)
    runtime = NativeRuntime(NativeConversationStore(InMemorySaver()))

    def run(model, text, confirmed=None):
        runtime.model_factory = lambda **_: model
        state = {"messages": [HumanMessage(content=text)]}
        if confirmed is not None:
            state["confirm_grant"] = confirmed
        return list(runtime.stream_events(state, config()))

    try:
        run(Model(), "通知全部标记已读")
        old = runtime.get_state(config()).values["pending_confirm"]
        run(Model(args={"scope": "selected", "ids": [102]}), "改成只标102")
        new = runtime.get_state(config()).values["pending_confirm"]
        assert new["specs"][0]["args"]["ids"] == [102] and old["token"] != new["token"]
        run(None, "", confirm.verify(old["token"], 7, 42))
        assert runtime.get_state(config()).values["pending_confirm"] == new
        model = Model("pending", "cancel_pending_operation", {})
        run(model, "算了取消吧")
        assert runtime.get_state(config()).values["pending_action"] is None
        run(None, "", confirm.verify(new["token"], 7, 42))
        assert not any(request.method == "POST" for request in notifications["requests"])
        assert new["token"] not in repr(model.prompts)
    finally:
        store.close()


@pytest.mark.parametrize("first_domain", ["favorites", "notifications"])
def test_replacement_across_domains_revokes_only_the_previous_card(
        notifications, monkeypatch, tmp_path, first_domain):
    store = idem.Ledger(str(tmp_path / "cross-domain.sqlite3"))
    monkeypatch.setattr(writes, "ledger_for", lambda saver: store)
    runtime = NativeRuntime(NativeConversationStore(InMemorySaver()))
    models = {"favorites": lambda: Model("favorites", "prepare_favorite_change", {
        "action": "add", "article_id": 19}), "notifications": Model}
    second_domain = "notifications" if first_domain == "favorites" else "favorites"
    try:
        for domain in [first_domain, second_domain]:
            runtime.model_factory = lambda **_: models[domain]()
            list(runtime.stream_events({"messages": [HumanMessage(content=domain)]}, config()))
            if domain == first_domain:
                old = runtime.get_state(config()).values["pending_confirm"]
        new = runtime.get_state(config()).values["pending_confirm"]
        assert confirm.verify(new["token"], 7, 42)["executor"] == second_domain
        stale = {"messages": [], "confirm_grant": confirm.verify(old["token"], 7, 42)}
        list(runtime.stream_events(stale, config()))
        assert runtime.get_state(config()).values["pending_confirm"] == new
        assert all(request.method == "GET" for request in notifications["requests"])
    finally:
        store.close()
