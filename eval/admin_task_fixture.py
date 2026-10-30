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
             "account": {"id": 20, "username": "青叶", "nickname": "青叶昵称",
                         "role": "user", "status": 0,
                         "mutedUntil": None}, "notices": [], "revision": 0}

    state["accounts"] = {20: state["account"]}

    def plan(intent, notice):
        matches = [row for row in state["accounts"].values() if all(
            value is None or row[key] == value for key, value in intent["target"].items())]
        if len(matches) != 1:
            return None
        row = {key: matches[0][key] for key in ("id", "username", "role", "status", "mutedUntil")}
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
            params = request.url.params
            rows = [row for row in state["accounts"].values() if row["role"] in {
                "admin", "user", "zako"} and all(
                value is None or str(row[key]) == value for key, value in
                (("id", params.get("id")), ("username", params.get("username"))))]
            if "query" in params:
                query = params["query"].casefold()
                rows = [row for row in rows if any(query in row[key].casefold()
                        for key in ("username", "nickname"))]
            total = len(rows)
            page, size = int(params.get("page", "1")), int(params.get("page_size", "20"))
            rows = [{key: row[key] for key in ("id", "username", "nickname", "role", "mutedUntil")}
                    | {"frozen": False, "muted": False, "quotaUsed": 0, "quotaLimit": 500}
                    for row in sorted(rows, key=lambda row: row["id"])[(page-1)*size:page*size]]
            return httpx.Response(200, json={"code": 200, "data": rows, "total": total})
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
            updated = {**state["accounts"][current["after"]["id"]], **deepcopy(current["after"])}
            state["accounts"][updated["id"]] = updated
            if updated["id"] == 20:
                state["account"] = updated
            state["writes"].append("role")
            data = {"changed": current["changed"], "beforeVersion": body["version"],
                    "state": current}
        return httpx.Response(200, json={"code": 200, "data": data})

    with httpx.Client(transport=httpx.MockTransport(handle)) as client, \
            patch.object(base, "_client", client), \
            patch.object(confirm.settings, "jwt_secret", SECRET):
        yield state
