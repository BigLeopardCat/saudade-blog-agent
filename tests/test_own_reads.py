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


def test_unread_counts_fetch_no_contents(mailbox):
    result = own_reads.get_unread_summary.invoke({}, config=config())
    assert json.loads(str(result)) == {"notifications": 2, "messages": 2, "total": 4}
    assert len(mailbox["requests"]) == 1
    assert mailbox["requests"][0].url.path.endswith("/notifications/summary")


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
    assert own_reads.get_unread_summary.invoke({}, config=config()).kind == "unavailable"


def test_network_failure_is_not_an_empty_mailbox(mailbox):
    mailbox["unavailable"] = True
    assert _list().kind == "unavailable"


def test_unexpected_server_body_is_not_forwarded(monkeypatch):
    response = {"folder": "inbox", "items": [{"id": 1, "toUserId": 7, "isRead": False,
                                                "content": "not requested"}], "hasMore": False}
    monkeypatch.setattr(base, "_own_get", lambda *args: response)
    assert "not requested" not in str(_list())
