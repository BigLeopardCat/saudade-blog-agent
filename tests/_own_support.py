"""Shared HTTP and identity fixtures for current-user domain contract tests."""

import json

import httpx
import pytest

from agent import confirm
from agent.principal import SOURCE_ASSERTION, Principal
from tools import base


def config(**values):
    return {"configurable": {"thread_id": "conv_42", "conversation_id": 42, "user_id": 7,
                             "principal": Principal(uid=7, role="user", source=SOURCE_ASSERTION),
                             **values}}


@pytest.fixture
def site(monkeypatch):
    state = {"favorites": [], "requests": [], "unavailable": False}
    monkeypatch.setattr(confirm.settings, "jwt_secret", "synthetic-confirmation-test-secret")

    def handle(request):
        state["requests"].append(request)
        if state["unavailable"]:
            return httpx.Response(503)
        if request.url.path.endswith("/notes/19"):
            data = {"noteId": 19, "noteTitle": "真实文章标题", "status": 1}
        elif request.url.path.endswith("/notes/search"):
            keyword = json.loads(request.content).get("keyword", "")
            data = ([{"noteId": 19, "noteTitle": "真实文章标题", "status": 1}]
                    if "真实" in keyword else [])
        elif request.url.path.endswith("/protected/favorites"):
            assert request.headers.get("Authorization", "").startswith("Bearer ")
            if request.method == "POST":
                assert json.loads(request.content) == {"noteId": 19}
                state["favorites"] = [{"noteId": 19, "title": "真实文章标题"}]
            data = list(state["favorites"])
        elif request.method == "DELETE" and request.url.path.endswith("/favorites/19"):
            state["favorites"] = []
            data = None
        else:
            return httpx.Response(404)
        return httpx.Response(200, json={"code": 200, "data": data})

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        monkeypatch.setattr(base, "_client", client)
        yield state


@pytest.fixture
def notifications(monkeypatch):
    state = {"items": [{"id": 101, "title": "审核结果", "isRead": False},
                       {"id": 102, "title": "系统公告", "isRead": False},
                       {"id": 103, "title": "已读通知", "isRead": True}],
             "requests": [], "unavailable": False, "ignore": set(),
             "new_on_write": False, "hidden_unread": 0}
    monkeypatch.setattr(confirm.settings, "jwt_secret", "synthetic-confirmation-test-secret")

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

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        monkeypatch.setattr(base, "_client", client)
        yield state
