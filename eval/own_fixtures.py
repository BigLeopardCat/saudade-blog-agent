"""Synthetic business API for own-data tests; never sends business HTTP requests."""

import json
import secrets
from contextlib import contextmanager
from unittest.mock import patch

import httpx

from agent import confirm, delegation
from tools import base

SECRET = secrets.token_urlsafe(32)


@contextmanager
def favorite_backend(article_id=19):
    state = {"favorites": [], "requests": [], "unavailable": False,
             "article": {"noteKey": article_id, "noteTitle": "真实文章标题", "status": 1}}

    def handle(request):
        state["requests"].append(request)
        if state["unavailable"]:
            return httpx.Response(503)
        if request.url.path.endswith(f"/notes/{article_id}"):
            data = state["article"]
        elif request.url.path.endswith("/notes/search"):
            keyword = json.loads(request.content).get("keyword", "")
            data = ([{"noteId": article_id, "noteTitle": "真实文章标题", "status": 1}]
                    if "真实" in keyword else [])
        elif request.url.path.endswith("/protected/favorites"):
            assert request.headers.get("Authorization", "").startswith("Bearer ")
            if request.method == "POST":
                assert json.loads(request.content) == {"noteId": article_id}
                state["favorites"] = [{"noteId": article_id, "title": "真实文章标题"}]
            data = list(state["favorites"])
        elif request.method == "DELETE" and request.url.path.endswith(f"/favorites/{article_id}"):
            state["favorites"] = []
            data = None
        else:
            return httpx.Response(404)
        return httpx.Response(200, json={"code": 200, "data": data})

    with httpx.Client(transport=httpx.MockTransport(handle)) as client, \
            patch.object(base, "_client", client), \
            patch.object(confirm.settings, "confirmation_secret", SECRET), \
            patch.object(confirm.settings, "agent_assertion_secret", SECRET), \
            patch.object(delegation, "credential", lambda uid: "offline-host-capability"):
        yield state


@contextmanager
def notification_backend():
    state = {"items": [{"id": 101, "title": "审核结果", "isRead": False},
                       {"id": 102, "title": "系统公告", "isRead": False},
                       {"id": 103, "title": "已读通知", "isRead": True}],
             "requests": [], "unavailable": False, "ignore": set(),
             "new_on_write": False, "hidden_unread": 0}
    for row in state["items"]:
        row.update(type="system", content="通知正文", link=None,
                   createdAt="2026-10-25 12:00:00")

    def handle(request):
        state["requests"].append(request)
        if "/protected/" in request.url.path:
            assert request.headers.get("Authorization", "").startswith("Bearer ")
        if state["unavailable"]:
            return httpx.Response(503)
        if request.method == "GET" and request.url.path.endswith("/favorites"):
            data = []
        elif request.method == "GET" and request.url.path.endswith("/notes/19"):
            data = {"noteId": 19, "noteTitle": "真实文章标题", "status": 1}
        elif request.method == "GET" and request.url.path.endswith("/notifications"):
            data = {"items": state["items"], "unread": state["hidden_unread"] + sum(
                not row["isRead"] for row in state["items"])}
        elif request.method == "POST" and request.url.path.endswith("/notifications/read"):
            payload = json.loads(request.content)
            assert payload["all"] is False and payload["ids"]
            for row in state["items"]:
                if row["id"] in payload["ids"] and row["id"] not in state["ignore"]:
                    row["isRead"] = True
            if state["new_on_write"]:
                state["items"].append({"id": 104, "title": "新到通知", "isRead": False})
            data = None
        else:
            return httpx.Response(404)
        return httpx.Response(200, json={"code": 200, "data": data})

    with httpx.Client(transport=httpx.MockTransport(handle)) as client, \
            patch.object(base, "_client", client), \
            patch.object(confirm.settings, "confirmation_secret", SECRET), \
            patch.object(confirm.settings, "agent_assertion_secret", SECRET), \
            patch.object(delegation, "credential", lambda uid: "offline-host-capability"):
        yield state



def inbox_overview(notifications, messages, limit=5, uid=7):
    """Fixture service projection, with no bodies or implicit read-state changes."""
    visible_notifications = [row for row in notifications if not row["isRead"]]
    visible_messages = [row for row in messages if not row["isRead"]]
    def header(row):
        title = row.get("title") or ""
        return {"id": row["id"], "title": title[:64], "titleTruncated": len(title)>64,
                "isRead": False, "createdAt": row.get("createdAt", "2026-11-05T08:00:00")}
    return {"userId": uid, "notifications": len(visible_notifications),
            "messages": len(visible_messages),
            "total": len(visible_notifications)+len(visible_messages), "limit": limit,
            "unreadOnly": True,
            "notificationItems": [{**header(row), "type": row.get("type", "system")}
                                  for row in visible_notifications[:limit]],
            "messageItems": [{**header(row), "fromUserId": row.get("fromUserId", 8),
                              "peerName": "fixture peer"} for row in visible_messages[:limit]],
            "notificationsHasMore": len(visible_notifications)>limit,
            "messagesHasMore": len(visible_messages)>limit}


def _mailbox_get(request, state):
    if request.url.path.endswith("/inbox/overview"):
        data = inbox_overview([{"id": 101, "title": "notice", "isRead": False},
                               {"id": 102, "title": "announcement", "isRead": False}],
                              state["items"], int(request.url.params.get("limit", "5")))
    elif request.url.path.endswith("/notifications/summary"):
        unread = sum(not row["isRead"] for row in state["items"])
        data = {"notifications": 2, "messages": unread, "total": 2 + unread}
    elif request.url.path.endswith("/messages/recent"):
        folder = request.url.params.get("folder", "inbox")
        limit = int(request.url.params.get("limit", "5"))
        source = state["items"] if folder == "inbox" else state["outbox"]
        fields = ("id", "fromUserId", "toUserId", "peerName", "title", "isRead", "createdAt")
        data = {"folder": folder,
                "items": [{key: row[key] for key in fields if key in row}
                          for row in source[:limit]], "hasMore": len(source) > limit}
    elif "/messages/" in request.url.path:
        key = int(request.url.path.rsplit("/", 1)[1])
        source = state["items"] + state["outbox"]
        data = next((dict(row) for row in source if row["id"] == key), None)
        if data is None:
            return httpx.Response(404)
        data.setdefault("content", "仅单封正文")
    elif request.url.path.endswith("/messages"):
        selected = request.url.params.get("ids")
        rows = state["items"]
        if selected:
            ids = {int(key) for key in selected.split(",")}
            rows = [row for row in rows if row["id"] in ids]
        elif request.url.params.get("unreadOnly") == "true":
            rows = [row for row in rows if not row["isRead"]]
        data = {"inbox": rows[:100], "inboxComplete": len(rows) <= 100,
                "outbox": state["outbox"],
                "unread": sum(not row["isRead"] for row in state["items"])}
    else:
        return httpx.Response(404)
    return httpx.Response(200, json={"code": 200, "data": data})


@contextmanager
def message_backend():
    """A received mailbox with a separate, unwritable outbox."""
    state = {"items": [{"id": 201, "toUserId": 7, "title": "欢迎来信", "isRead": False},
                       {"id": 202, "toUserId": 7, "title": "回访来信", "isRead": False},
                       {"id": 203, "toUserId": 7, "title": "旧来信", "isRead": True}],
             "outbox": [{"id": 301, "fromUserId": 7, "toUserId": 8,
                         "title": "发出的信", "isRead": False}],
             "requests": [], "unavailable": False, "ignore": set(), "new_on_write": False}

    def handle(request):
        state["requests"].append(request)
        assert request.headers.get("Authorization", "").startswith("Bearer ")
        if state["unavailable"]:
            return httpx.Response(503)
        if request.method == "GET":
            return _mailbox_get(request, state)
        elif request.method == "POST" and request.url.path.endswith("/messages/read"):
            payload = json.loads(request.content)
            assert payload["all"] is False and payload["ids"]
            for row in state["items"]:
                if row["id"] in payload["ids"] and row["id"] not in state["ignore"]:
                    row["isRead"] = True
            if state["new_on_write"]:
                state["items"].append({"id": 204, "toUserId": 7,
                                       "title": "新到来信", "isRead": False})
            data = None
        else:
            return httpx.Response(404)
        return httpx.Response(200, json={"code": 200, "data": data})

    with httpx.Client(transport=httpx.MockTransport(handle)) as client, \
            patch.object(base, "_client", client), \
            patch.object(confirm.settings, "confirmation_secret", SECRET), \
            patch.object(confirm.settings, "agent_assertion_secret", SECRET), \
            patch.object(delegation, "credential", lambda uid: "offline-host-capability"):
        yield state
