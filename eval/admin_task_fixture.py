"""Synthetic account and notice API for full native task evaluation."""

import hashlib
import json
from contextlib import contextmanager
from copy import deepcopy
from unittest.mock import patch

import httpx

from agent import confirm
from agent.domains.notices.targets import DEFAULT_TITLE
from eval.own_fixtures import SECRET
from tools import base


@contextmanager
def backend():
    state = {"caller_role": "admin", "requests": [], "unsupported_reads": [], "writes": [],
             "account": {"id": 20, "username": "青叶", "role": "user", "status": 0,
                         "mutedUntil": None}, "notices": [], "revision": 0}

    def plan(intent, notice):
        row = state["account"]
        if any(value is not None and row[key] != value
               for key, value in intent["target"].items()):
            return None
        version = hashlib.sha256(json.dumps([row, intent, state["revision"]],
                                            sort_keys=True).encode()).hexdigest()
        if notice:
            return {"version": version, "recipient": {key: row[key] for key in
                                                      ("id", "username", "role")},
                    "title": intent.get("title") or DEFAULT_TITLE, "content": intent["content"]}
        if intent["action"] != "set_role":
            return None
        after = {**row, "role": intent["role"]}
        changed = row != after
        return {"version": version, "before": deepcopy(row), "after": after,
                "changed": changed, "revokeSessions": changed, "notificationCount": int(changed)}

    def handle(request):
        state["requests"].append(request)
        path = request.url.path
        if request.method == "GET" and path == "/api/protected/accounts":
            account = state["account"]
            matches = all(value is None or str(account[key]) == value for key, value in
                          (("id", request.url.params.get("id")),
                           ("username", request.url.params.get("username"))))
            rows = [{key: account[key] for key in ("id", "username", "role", "mutedUntil")}
                    | {"frozen": False, "muted": False, "quotaUsed": 0, "quotaLimit": 500}]
            return httpx.Response(200, json={"code": 200,
                                            "data": rows if matches else [],
                                            "total": int(matches)})
        notice = path.startswith("/api/protected/notice-actions")
        if request.method != "POST" or path not in {
                "/api/protected/notice-actions", "/api/protected/notice-actions/prepare",
                "/api/protected/account-actions", "/api/protected/account-actions/prepare"}:
            state["unsupported_reads"].append(str(request.url))
            return httpx.Response(404)
        body = json.loads(request.content)
        preparation = path.endswith("/prepare")
        current = plan(body if preparation else body["intent"], notice)
        if current is None or not preparation and current["version"] != body["version"]:
            return httpx.Response(200, json={"code": 409, "message": "目标或版本不匹配"})
        if preparation:
            return httpx.Response(200, json={"code": 200, "data": current})
        state["revision"] += 1
        if notice:
            state["notices"].append(deepcopy(current))
            state["writes"].append("notice")
            data = {"notificationId": 101, "delivered": True, "state": current}
        else:
            state["account"] = deepcopy(current["after"])
            state["writes"].append("role")
            data = {"changed": current["changed"], "beforeVersion": body["version"],
                    "state": current}
        return httpx.Response(200, json={"code": 200, "data": data})

    with httpx.Client(transport=httpx.MockTransport(handle)) as client, \
            patch.object(base, "_client", client), \
            patch.object(confirm.settings, "jwt_secret", SECRET):
        yield state
