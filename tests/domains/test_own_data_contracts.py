"""Own-data operations use precise owned targets and authoritative facts."""


import pytest
from _own_support import config

from agent import confirm
from agent.domains.favorites import preparation
from agent.domains.notifications import preparation as notices
from agent.runtime.results import TurnResult
from tools import own_data


def test_hidden_article_favorite_can_be_removed_without_public_or_collection_reads(site):
    site["favorites"] = [{"noteId": 19, "title": "previously public"}]
    site["article_available"] = False
    prepared = preparation.prepare_favorite_change.invoke(
        {"action": "remove", "article_id": 19}, config=config())
    assert isinstance(prepared, TurnResult) and "pending_confirm" in prepared.execution
    assert "previously public" not in prepared.reply
    grant = confirm.verify(prepared.execution["pending_confirm"]["token"], 7, 42)
    assert grant["specs"] == [{"tool": "remove_favorite", "args": {"article_id": 19}}]
    result = own_data.remove_favorite.invoke({"article_id": 19}, config=config())
    assert result.kind == "ok" and result.meta["changed"] is True
    assert not site["favorites"]
    assert [(request.method, request.url.path) for request in site["requests"]] == [
        ("GET", "/api/protected/favorites/19"), ("DELETE", "/api/protected/favorites/19")]


def test_nonpublic_article_cannot_create_a_new_favorite(site):
    site["article_available"] = False
    result = preparation.prepare_favorite_change.invoke(
        {"action": "add", "article_id": 19}, config=config())
    assert result.kind == "unavailable" and not site["favorites"]
    assert [request.method for request in site["requests"]] == ["GET"]


@pytest.mark.parametrize("replacement", [
    {"ownerUserId": 8}, {"ownerUserId": True}, {"noteId": 20}, {"noteId": True},
    {"isFavorite": 0}, {"articleAvailable": "true"}, {"title": None},
])
def test_preparation_does_not_trust_malformed_owned_state(site, replacement):
    site["state_override"] = {"ownerUserId": 7, "noteId": 19, "isFavorite": False,
                              "articleAvailable": True, "title": "public", **replacement}
    result = preparation.prepare_favorite_change.invoke(
        {"action": "add", "article_id": 19}, config=config())
    assert result.kind == "unavailable" and not site["favorites"]


@pytest.mark.parametrize("replacement", [None, "success", {}, {"ownerUserId": 8},
                                         {"noteId": True}, {"changed": 1},
                                         {"isFavorite": False}])
def test_write_text_or_malformed_receipt_never_certifies_a_favorite(monkeypatch, replacement):
    valid = {"ownerUserId": 7, "noteId": 19, "isFavorite": True, "changed": True}
    payload = ({**valid, **replacement}
               if isinstance(replacement, dict) and replacement else replacement)
    monkeypatch.setattr(own_data, "_own_post", lambda *args: payload)
    monkeypatch.setattr(own_data, "_own_get", lambda *args: pytest.fail("no read-back is needed"))
    result = own_data.add_favorite.invoke({"article_id": 19}, config=config())
    assert result.kind == "unavailable"


def test_old_owned_notification_ids_are_resolved_without_fetching_history(notifications):
    notifications["items"] = [{"id": index, "title": f"notice {index}", "isRead": False}
                              for index in range(200, 0, -1)]
    result = notices.prepare_notification_read.invoke(
        {"scope": "selected", "ids": [1]}, config=config())
    assert isinstance(result, TurnResult)
    grant = confirm.verify(result.execution["pending_confirm"]["token"], 7, 42)
    assert grant["specs"][0]["args"] == {"ids": [1], "all": False}
    request, = notifications["requests"]
    assert request.url.path.endswith("/notifications/state") and request.url.params["ids"] == "1"


def test_unbounded_unread_state_cannot_be_signed_as_a_complete_selection(notifications):
    notifications["items"] = [{"id": index, "title": str(index), "isRead": False}
                              for index in range(125)]
    notifications["items"][0]["id"] = 999
    result = notices.prepare_notification_read.invoke({"scope": "all_unread"}, config=config())
    assert not isinstance(result, TurnResult) and result.kind == "unavailable"
    assert all(request.method == "GET" for request in notifications["requests"])


def test_idempotent_favorite_write_uses_the_server_fact_without_pre_or_post_reads(site):
    site["favorites"] = [{"noteId": 19, "title": "public"}]
    result = own_data.add_favorite.invoke({"article_id": 19}, config=config())
    assert result.kind == "ok" and result.meta["changed"] is False
    assert [(request.method, request.url.path) for request in site["requests"]] == [
        ("POST", "/api/protected/favorites")]
