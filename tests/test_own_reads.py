"""Own-data requests separate counts, bounded metadata and requested bodies."""

import json

import pytest
from _own_support import config
from pydantic import ValidationError

from agent.domains import own_reads
from agent.principal import Principal
from tools import base


def _list(**args):
    return own_reads.list_my_messages.invoke(args, config=config())


def test_explicit_counts_mode_fetches_no_headers_or_bodies(mailbox):
    result = own_reads.get_inbox_overview.invoke({"mode": "counts_only"}, config=config())
    assert json.loads(str(result)) == {"notifications": 2, "messages": 2, "total": 4,
                                       "itemsIncluded": False}
    assert len(mailbox["requests"]) == 1
    assert mailbox["requests"][0].url.path.endswith("/notifications/summary")


def test_registered_counter_has_the_same_bounded_contract(mailbox):
    tools = {tool.name: tool for tool in base.get_all_tools()}
    value = tools["get_inbox_overview"].invoke({"mode": "counts_only"}, config=config())
    assert json.loads(str(value)) == {"notifications": 2, "messages": 2, "total": 4,
                                      "itemsIncluded": False}
    assert "get_unread_summary" not in tools and "get_unread_counts" not in tools
    assert len(mailbox["requests"]) == 1


def test_default_list_is_small_and_omits_bodies_and_outbox(mailbox):
    mailbox["items"] = [{"id": i, "toUserId": 7, "title": str(i), "isRead": False,
                         "content": "not requested" * 500} for i in range(1, 21)]
    result = json.loads(str(_list()))
    assert len(result["items"]) == 5 and result["hasMore"] is True
    assert all("content" not in row for row in result["items"])
    assert "outbox" not in result and "not requested" not in str(result)
    assert dict(mailbox["requests"][-1].url.params) == {"folder": "inbox", "limit": "5"}
    assert all(r.method == "GET" for r in mailbox["requests"])


def test_requested_body_is_single_owned_message_without_marking_read(mailbox):
    result = json.loads(str(_list(message_id=201)))
    assert result["id"] == 201 and result["content"] == "仅单封正文"
    assert len(mailbox["requests"]) == 1
    assert mailbox["requests"][0].url.path.endswith("/messages/201")
    assert not mailbox["items"][0]["isRead"]
    result = json.loads(str(_list(folder="outbox")))
    assert [row["id"] for row in result["items"]] == [301]


@pytest.mark.parametrize("args", [{"limit": 0}, {"limit": 11}, {"limit": True},
                                  {"message_id": -1}, {"folder": "any"}])
def test_bounds_reject_before_http(mailbox, args):
    with pytest.raises(ValidationError):
        _list(**args)
    assert not mailbox["requests"]


def test_unverified_identity_and_foreign_rows_never_become_read_facts(mailbox):
    result = own_reads.list_my_messages.invoke({}, config=config(
        principal=Principal(uid=7, role="user")))
    assert result.kind == "unavailable" and not mailbox["requests"]
    mailbox["items"][0]["toUserId"] = 8
    assert _list().kind == "unavailable"
    assert _list(message_id=201).kind == "unavailable"


@pytest.mark.parametrize("data", [None, {"notifications": True, "messages": 1, "total": 2},
                                  {"notifications": 1, "messages": 1, "total": 4}])
def test_bad_counts_are_not_zero(monkeypatch, data):
    monkeypatch.setattr(base, "_own_get", lambda *args: data)
    assert own_reads.get_inbox_overview.invoke({}, config=config()).kind == "unavailable"


def test_network_failure_is_not_an_empty_mailbox(mailbox):
    mailbox["unavailable"] = True
    assert _list().kind == "unavailable"


def test_unexpected_server_body_is_not_forwarded(monkeypatch):
    response = {"folder": "inbox", "items": [{"id": 1, "toUserId": 7, "isRead": False,
                                                "content": "not requested"}], "hasMore": False}
    monkeypatch.setattr(base, "_own_get", lambda *args: response)
    assert "not requested" not in str(_list())


def test_default_overview_returns_owned_unread_headers_without_bodies(mailbox):
    result = own_reads.get_inbox_overview.invoke({}, config=config())
    assert result.kind == "ok"
    data = result.data
    assert data["notifications"] == data["messages"] == 2 and data["total"] == 4
    assert data["unreadOnly"] and data["itemsIncluded"]
    assert len(data["notificationItems"]) == len(data["messageItems"]) == 2
    assert all(row["isRead"] is False and "content" not in row
               for row in data["notificationItems"] + data["messageItems"])
    assert "userId" not in data and not data["notificationsHasMore"]
    assert len(mailbox["requests"]) == 1
    assert mailbox["requests"][0].url.path.endswith("/inbox/overview")


@pytest.mark.parametrize("fault", ["recipient", "missing_items", "counts", "read", "duplicates"])
def test_overview_rejects_unowned_or_incomplete_evidence(monkeypatch, fault):
    from eval.own_fixtures import inbox_overview
    data = inbox_overview([{"id": 1, "title": "header", "isRead": False}], [])
    if fault == "recipient":
        data["userId"] = 8
    elif fault == "missing_items":
        data["notificationItems"] = []
    elif fault == "counts":
        data["total"] = 5
    elif fault == "read":
        data["notificationItems"][0]["isRead"] = True
    else:
        data["notificationItems"] *= 2
        data.update(notifications=2, total=2)
    monkeypatch.setattr(base, "_own_get", lambda *args: data)
    assert own_reads.get_inbox_overview.invoke({}, config=config()).kind == "unavailable"


def test_inbox_discovery_loads_authorized_overview_before_one_read(mailbox):
    from langchain_core.messages import AIMessage, HumanMessage
    from langgraph.checkpoint.memory import InMemorySaver
    from test_confirmation_continuation import Model, call

    from agent.products.blog.application import BlogApplication
    from agent.runtime.checkpoint_conversations import CheckpointConversationStore
    from agent.runtime.service import AgentRuntime

    model = Model(
        AIMessage(content="", tool_calls=[call("load_inbox_info_tools", "load")]),
        AIMessage(content="", tool_calls=[call("get_inbox_overview", "read", mode="counts_only")]),
        AIMessage(content="共有4条未读。"),
    )
    runtime = AgentRuntime(CheckpointConversationStore(InMemorySaver()),
                           application=BlogApplication(), model_factory=lambda **_: model)
    result = list(runtime.stream_events({"messages": [HumanMessage(content="只查未读数量")]},
                                        config()))[-1]
    assert result.reply == "共有4条未读。"
    assert all(t["function"]["name"] != "get_inbox_overview" for t in model.menus[0])
    assert any(t["function"]["name"] == "get_inbox_overview" for t in model.menus[1])
    assert model.menus[1] == model.menus[2]
    assert len(mailbox["requests"]) == 1
    assert mailbox["requests"][0].url.path.endswith("/notifications/summary")


def test_empty_inbox_is_complete_only_within_the_requested_folder(mailbox):
    mailbox["items"] = []
    data = _list().data
    assert data["items"] == [] and data["folder"] == "inbox"
    assert data["collectionComplete"] is True and data["returnedCount"] == 0
    assert data["unreadTotal"] is None and data["limit"] == 5
    sent = _list(folder="outbox").data
    assert sent["folder"] == "outbox" and sent["returnedCount"] == 1
    assert len(mailbox["requests"]) == 2


def test_bounded_message_preview_does_not_claim_full_folder_coverage(mailbox):
    mailbox["items"] *= 3
    data = _list(limit=1).data
    assert data["hasMore"] is True and data["collectionComplete"] is False
    assert data["returnedCount"] == data["limit"] == 1 and data["unreadTotal"] is None
