"""Shared HTTP and identity fixtures for favorite-domain contract tests."""

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
