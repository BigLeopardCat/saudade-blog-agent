"""Synthetic post, folder and article-deletion service for native contract evaluations."""

import hashlib
import json
from contextlib import contextmanager
from copy import deepcopy
from unittest.mock import patch

import httpx

from agent import confirm, delegation
from eval.own_fixtures import SECRET
from tools import base


def digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


@contextmanager
def backend():
    state = {
        "caller_role": "admin",
        "requests": [],
        "writes": 0,
        "posts": {11: {"id": 11, "title": "原说说", "content": "原正文", "authorId": 7}},
        "folders": {
            21: {"id": 21, "parentId": 0, "name": "父目录", "imageCount": 1},
            22: {"id": 22, "parentId": 0, "name": "空目录", "imageCount": 0},
        },
        "articles": {
            key: {
                "id": key,
                "title": f"文章 {key}",
                "version": str(key % 10) * 64,
                "draftCount": 1,
                "commentCount": 2,
                "favoriteCount": 3,
            }
            for key in (31, 32)
        },
    }

    def post_plan(intent):
        action = intent["action"]
        before = deepcopy(state["posts"].get(intent.get("id")))
        if action != "create" and before is None:
            raise ValueError("post missing")
        after = (
            deepcopy(before) if before else {"id": None, "title": "", "content": "", "authorId": 7}
        )
        if action == "delete":
            after = None
        else:
            after.update({k: v for k, v in intent["fields"].items() if v is not None})
        return {"version": digest([intent, before]), "before": before, "after": after}

    def folder_plan(intent):
        parent = None
        before = None
        if intent["action"] == "create":
            if intent["parentId"]:
                parent = deepcopy(state["folders"].get(intent["parentId"]))
                if parent is None:
                    raise ValueError("parent missing")
        else:
            before = deepcopy(state["folders"].get(intent["id"]))
            if before is None or before["imageCount"] or state.get("folder_busy"):
                raise ValueError("folder missing or not empty")
        return {
            "version": digest([intent, before, parent]),
            "before": before,
            "parent": parent,
            "childCount": 0,
        }

    def handle(request):
        state["requests"].append(request)
        path = request.url.path
        if request.method == "GET" and path == "/api/protected/talk-actions":
            data = {"items": list(state["posts"].values()), "total": len(state["posts"]), "page": 1}
            return httpx.Response(200, json={"code": 200, "data": data})
        if request.method == "GET" and path == "/api/protect/gallery/folders":
            parent = int(request.url.params.get("parentId", "0"))
            before = int(request.url.params.get("before", "0"))
            limit = int(request.url.params.get("limit", "31"))
            data = [
                row
                for row in state["folders"].values()
                if row["parentId"] == parent and row["id"] > before
            ]
            return httpx.Response(
                200, json={"code": 200, "data": sorted(data, key=lambda row: row["id"])[:limit]}
            )
        if request.method == "GET" and path == "/api/protect/gallery/images":
            return httpx.Response(
                200, json={"code": 200, "data": {"items": [], "nextCursor": None}}
            )
        if request.method == "GET" and path == "/api/protected/articles":
            selected = request.url.params.get("id")
            rows = [
                dict(
                    row,
                    isPublic=True,
                    status="public",
                    isTop=0,
                    categoryId=None,
                    categoryName=None,
                    tags=[],
                )
                for row in state["articles"].values()
                if selected is None or str(row["id"]) == selected
            ]
            page, limit = (
                int(request.url.params.get("page", "1")),
                int(request.url.params.get("page_size", "20")),
            )
            return httpx.Response(
                200,
                json={
                    "code": 200,
                    "data": rows[(page - 1) * limit : page * limit],
                    "total": len(rows),
                },
            )
        payload = json.loads(request.content) if request.content else {}
        domain = (
            "talks"
            if path.startswith("/api/protected/talk-actions")
            else "folders"
            if path.startswith("/api/protect/gallery/folder-actions")
            else "articles"
            if path.startswith("/api/protected/article-deletions")
            else None
        )
        if domain is None:
            state.setdefault("unsupported_reads", []).append(path)
            return httpx.Response(404)
        prepare = path.endswith("/prepare")
        try:
            if domain == "articles":
                ids = (
                    payload["articleIds"] if prepare else [item["id"] for item in payload["items"]]
                )
                rows = [deepcopy(state["articles"][identifier]) for identifier in sorted(ids)]
                if prepare:
                    data = rows
                else:
                    if payload["items"] != [
                        {"id": row["id"], "version": row["version"]} for row in rows
                    ]:
                        raise ValueError("stale article batch")
                    for row in rows:
                        del state["articles"][row["id"]]
                    data = {"deleted": True, "items": rows}
            else:
                intent = payload if prepare else payload["intent"]
                plan = post_plan(intent) if domain == "talks" else folder_plan(intent)
                if prepare:
                    data = plan
                else:
                    if payload["version"] != plan["version"]:
                        raise ValueError("stale target")
                    if domain == "talks":
                        changed = plan["before"] != plan["after"]
                        if plan["after"] is None:
                            del state["posts"][intent["id"]]
                        else:
                            plan["after"]["id"] = plan["after"]["id"] or 12
                            state["posts"][plan["after"]["id"]] = deepcopy(plan["after"])
                        data = {
                            "changed": changed,
                            "beforeVersion": payload["version"],
                            "state": plan,
                        }
                    else:
                        folder = None
                        if intent["action"] == "create":
                            folder = {
                                "id": 23,
                                "name": intent["name"],
                                "parentId": intent["parentId"],
                                "imageCount": 0,
                            }
                            state["folders"][23] = deepcopy(folder)
                        else:
                            del state["folders"][intent["id"]]
                        data = {
                            "changed": True,
                            "beforeVersion": payload["version"],
                            "folder": folder,
                        }
        except (ValueError, KeyError):
            return httpx.Response(200, json={"code": 409, "message": "target changed", "data": {}})
        if not prepare:
            state["writes"] += 1
            if state.get("lost"):
                raise httpx.ReadTimeout("effect committed, reply lost")
            if state.get("tamper"):
                if domain == "talks":
                    data["state"]["after"]["authorId"] = 99
                elif domain == "folders":
                    data["folder"]["parentId"] = 999
                else:
                    data["items"] = data["items"][:1]
        return httpx.Response(200, json={"code": 200, "data": data})

    with (
        httpx.Client(transport=httpx.MockTransport(handle)) as client,
        patch.object(base, "_client", client),
        patch.object(confirm.settings, "confirmation_secret", SECRET),
        patch.object(delegation, "credential", lambda uid: "offline-host-capability"),
    ):
        yield state
