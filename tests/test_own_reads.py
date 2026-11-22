"""Own-data requests separate counts, bounded metadata and requested bodies."""

import json

import pytest
from _own_support import config
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver
from pydantic import ValidationError
from test_confirmation_continuation import Model, call

from agent import authz
from agent.domains import own_reads
from agent.principal import Principal
from agent.products.blog.application import BlogApplication
from agent.runtime import memory
from agent.runtime.checkpoint_conversations import CheckpointConversationStore
from agent.runtime.service import AgentRuntime
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


def test_authorized_inbox_reads_from_the_first_model_menu(mailbox):
    model = Model(
        AIMessage(content="", tool_calls=[call("get_inbox_overview", "read", mode="counts_only")]),
        AIMessage(content="共有4条未读。"),
    )
    runtime = AgentRuntime(CheckpointConversationStore(InMemorySaver()),
                           application=BlogApplication(), model_factory=lambda **_: model)
    result = list(runtime.stream_events({"messages": [HumanMessage(content="只查未读数量")]},
                                        config()))[-1]
    assert result.reply == "共有4条未读。"
    names = {t["function"]["name"] for t in model.menus[0]}
    assert {"get_inbox_overview", "load_notifications_info_tools",
            "load_messages_info_tools"} <= names
    assert "load_inbox_info_tools" not in names
    assert len(model.menus) == 2 and model.menus[0] == model.menus[1]
    read = next(m for m in model.prompts[-1] if isinstance(m, ToolMessage))
    assert read.status == "success" and json.loads(read.content)["total"] == 4
    assert len(mailbox["requests"]) == 1
    assert mailbox["requests"][0].url.path.endswith("/notifications/summary")
    assert not runtime.store.load(config()).tool_domains["domains"]


@pytest.mark.parametrize("denial", ["body_identity", "anonymous", "foreign_actor", "own_scope"])
def test_inbox_stays_hidden_and_never_reads_without_current_authority(mailbox, monkeypatch, denial):
    if denial == "body_identity":
        cfg = config(principal=Principal(7, "user"))
    elif denial == "foreign_actor":
        cfg = config(user_id=8)
    elif denial == "anonymous":
        cfg = config(principal=None)
    else:
        cfg = config()
        monkeypatch.setitem(authz._ROLE_SCOPES, "user",
                            authz.scopes_for("user") - {authz.SCOPE_READ_OWN})
    model = Model(
        AIMessage(content="", tool_calls=[call("get_inbox_overview", "read", mode="counts_only")]),
        AIMessage(content="无法查询未读收件。"),
    )
    runtime = AgentRuntime(CheckpointConversationStore(None),
                           application=BlogApplication(), model_factory=lambda **_: model)
    list(runtime.stream_events({"messages": [HumanMessage(content="只查未读数量")]}, cfg))
    names = {t["function"]["name"] for t in model.menus[0]}
    assert "get_inbox_overview" not in names and "load_inbox_info_tools" not in names
    read = next(m for m in model.prompts[-1] if isinstance(m, ToolMessage))
    assert read.status == "error" and not mailbox["requests"]


def test_old_checkpoint_replays_discovery_and_uses_current_inbox_tools(mailbox):
    cfg = config()
    store = CheckpointConversationStore(InMemorySaver())
    old_names = ["load_inbox_info_tools", "load_utilities_info_tools"]
    messages = [AIMessage(content="", tool_calls=[call(name, name) for name in old_names]),
                *(ToolMessage(content="工具已加载。", name=name, tool_call_id=name)
                  for name in old_names)]
    store.update_state(cfg, {"messages": memory.turn_messages(
        [HumanMessage(content="看看时间和未读收件。")], messages, "已查看。",
        cfg["configurable"]["principal"]), "tool_domains": {
        "scope": {"uid": 7, "role": "user", "conversation_id": 42},
        "domains": [{"domain": name, "turn_id": "old-turn"} for name in ("inbox", "utilities")],
    }})
    model = Model(
        AIMessage(content="", tool_calls=[call(name, f"legacy-{name}") for name in old_names]),
        AIMessage(content="", tool_calls=[call("get_inbox_overview", "read", mode="counts_only")]),
        AIMessage(content="共有4条未读。"),
    )
    runtime = AgentRuntime(store, application=BlogApplication(), model_factory=lambda **_: model)
    result = list(runtime.stream_events({"messages": [HumanMessage(content="再查一下数量。")]},
                                        cfg))[-1]
    assert result.reply == "共有4条未读。"
    assert {m.name for m in model.prompts[0] if isinstance(m, ToolMessage)} == set(old_names)
    legacy_receipts = [m for m in model.prompts[-1] if isinstance(m, ToolMessage)
                       and m.tool_call_id.startswith("legacy-")]
    assert len(legacy_receipts) == 2 and all(m.status == "success" for m in legacy_receipts)
    assert model.menus[0] == model.menus[1] == model.menus[2]
    names = [t["function"]["name"] for t in model.menus[0]]
    assert not set(old_names).intersection(names)
    assert names.count("get_inbox_overview") == names.count("get_current_time") == 1
    assert len(mailbox["requests"]) == 1


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
