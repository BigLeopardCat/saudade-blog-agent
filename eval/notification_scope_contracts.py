"""Real-model recipient-copy selection against a synthetic preparation service."""

import hashlib
import json
from contextlib import contextmanager
from unittest.mock import patch

import httpx

from agent import confirm
from eval.own_fixtures import SECRET, inbox_overview
from tools import base


@contextmanager
def backend():
    rows = [{"id": 301, "type": "system", "title": "额度已重置"},
            *({"id": key, "type": "announcement", "title": f"公告副本{key}"}
              for key in range(302, 321))]
    titles = ("回归部署完成", "晚安提醒", "评论通知", "系统维护", "节日问候")
    for row, title in zip(rows[-5:], titles):
        row["title"] = title
    for row in rows:
        row.update(content=row["title"], isRead=False, link=None, createdAt="2026-11-05 07:00:00")
    state = {"requests": []}

    def handle(request):
        state["requests"].append(request)
        if request.method == "GET" and request.url.path.endswith("/inbox/overview"):
            data = inbox_overview(list(reversed(rows)), [],
                                  int(request.url.params.get("limit", "5")))
        elif request.method == "GET" and request.url.path.endswith("/notifications/summary"):
            data = {"notifications": len(rows), "messages": 0, "total": len(rows)}
        elif request.method == "GET" and request.url.path.endswith("/notifications"):
            data = {"items": rows, "unread": len(rows)}
        elif (request.method == "POST"
              and request.url.path.endswith("/notification-deletions/prepare")):
            args = json.loads(request.content)
            selected = [row for row in rows if args["kind"] == "all"
                        or (args["kind"] == "announcement") == (row["type"] == "announcement")]
            ids = [row["id"] for row in selected]
            data = {"version": hashlib.sha256(json.dumps(ids).encode()).hexdigest(), "ids": ids,
                    "items": [{"id": row["id"], "title": row["title"]} for row in selected],
                    "count": len(ids), "associatedNotifications": 0}
        else:
            return httpx.Response(404)
        return httpx.Response(200, json={"code": 200, "data": data})

    with httpx.Client(transport=httpx.MockTransport(handle)) as client, \
            patch.object(base, "_client", client), \
            patch.object(confirm.settings, "confirmation_secret", SECRET):
        yield state


def contract(message, kind, ids):
    def check(turn, state):
        result = turn(message)
        if len(result["confirm_tokens"]) != 1 or result["exec_rows"]:
            raise ValueError("expected one preparation without execution")
        grant = confirm.verify(result["confirm_tokens"][0], 7, 42)
        args = grant["specs"][0]["args"] if grant else {}
        if args.get("kind") != kind or args.get("ids") != ids:
            raise ValueError("notification-copy selection changed the requested scope")
        if any(request.method != "GET" and not request.url.path.endswith("/prepare")
               for request in state["requests"]):
            raise ValueError("preparation wrote business data")
    return check



def readable_overview(turn, state):
    result = turn("我有哪些未读通知？")
    events = result.get("native_events") or []
    calls = [event for event in events if event.get("event") == "call"
             and event.get("name") == "get_inbox_overview" and event.get("kind") == "ok"]
    data = json.loads(calls[-1]["result"]) if calls else {}
    items = data.get("notificationItems", [])
    if (not data.get("itemsIncluded") or len(items) != 5
            or sum(row["title"] in result["text"] for row in items) < 2):
        raise ValueError("unread request did not deliver readable notification headers")
    if result["confirm_tokens"] or result["exec_rows"]:
        raise ValueError("read-only overview prepared or executed a mutation")


CASES = [
    ("inbox_readable_overview", backend, readable_overview),
    ("notification_delete_all_copies", backend, contract(
        "清空我个人中心的全部通知，只删除我收到的副本，保留全站公告和其他人的通知。",
        "all", list(range(301, 321)))),
    ("notification_delete_announcement_copies", backend, contract(
        "只删除我个人中心收到的公告副本，保留个人通知、全站公告和其他人的通知。",
        "announcement", list(range(302, 321)))),
    ("notification_delete_personal_copies", backend, contract(
        "只删除我个人中心收到的个人通知，保留我收到的公告副本、全站公告和其他人的通知。",
        "notice", [301])),
]
