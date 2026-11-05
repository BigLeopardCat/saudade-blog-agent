"""Synthetic atomic tag batches with shared newly-created tag identities."""

import hashlib
import json
from contextlib import contextmanager
from copy import deepcopy
from unittest.mock import patch

import httpx

from agent import confirm
from eval.own_fixtures import SECRET
from tools import base


def article_page(state, params):
    ident = params.get("id")
    rows = [{"id": row["id"], "title": row["title"], "status": "public", "isPublic": True,
             "isTop": 0, "categoryId": None, "categoryName": None, "tags": row["tags"]}
            for row in state["rows"].values() if ident is None or str(row["id"]) == ident]
    page, limit = int(params.get("page", 1)), int(params.get("page_size", 20))
    return httpx.Response(200, json={"code": 200,
        "data": rows[(page - 1) * limit:page * limit], "total": len(rows)})


def tag_page(state, params):
    rows = [{"id": tag["id"], "name": tag["name"], "level": tag["level"],
             "parentId": tag["parentId"], "publicArticleCount": sum(
                 any(t["id"] == tag["id"] for t in row["tags"])
                 for row in state["rows"].values())}
            for tag in state["tags"].values()]
    level = params.get("level")
    rows = [row for row in rows if level is None or str(row["level"]) == level]
    return httpx.Response(200, json={"code": 200, "data": rows, "total": len(rows)})


@contextmanager
def backend():
    state = {"caller_role": "admin", "requests": [], "unsupported_reads": [], "writes": [],
             "tags": {}, "rows": {i: {"id": i, "title": f"文章{i}", "tags": [], "revision": 0}
                                  for i in (11, 12, 13)}}

    def plan(ident, intent):
        row = state["rows"][ident]
        before = deepcopy(row["tags"])
        after = [] if intent["replace"] is not None else deepcopy(before)
        after = [tag for tag in after if tag["name"] not in intent["remove"]]
        for name in intent["replace"] if intent["replace"] is not None else intent["add"]:
            if not any(tag["name"] == name for tag in after):
                after.append(deepcopy(state["tags"].get(name) or {
                    "id": None, "name": name, "level": 1, "parentId": None,
                    "color": intent["color"]}))
        version = hashlib.sha256(json.dumps(
            [row, after, intent], sort_keys=True).encode()).hexdigest()
        return {"id": ident, "title": row["title"], "version": version,
                "before": before, "after": after}

    def apply(plans, ids):
        results = []
        for row in plans:
            changed = row["before"] != row["after"]
            for tag in row["after"]:
                if tag["id"] is None:
                    if tag["name"] not in state["tags"]:
                        state["tags"][tag["name"]] = {**tag, "id": 100 + len(state["tags"])}
                    tag.update(state["tags"][tag["name"]])
            state["rows"][row["id"]]["tags"] = deepcopy(row["after"])
            results.append({"beforeVersion": row["version"], "changed": changed, "state": row})
        state["writes"].append(ids)
        if state.get("lost"):
            raise httpx.ReadTimeout("synthetic lost response")
        if state.get("duplicate"):
            results[-1] = results[0]
        if state.get("incomplete"):
            results.pop()
        return results

    def handle(request):
        state["requests"].append(request)
        path = request.url.path
        root = "/api/protected/article-tag-actions/"
        if (request.method == "GET" and path == "/api/protected/articles"
                and set(request.url.params) <= {"id", "page", "page_size"}):
            return article_page(state, request.url.params)
        if request.method == "GET" and path == "/api/public/tags":
            return tag_page(state, request.url.params)
        if request.method != "POST" or not path.startswith(root):
            state["unsupported_reads"].append(str(request.url))
            return httpx.Response(404)
        body = json.loads(request.content)
        preparing = path.endswith("/prepare")
        batch = path in (root + "batch/prepare", root + "batch")
        ids = body["article_ids"] if preparing and batch else (
            [item["id"] for item in body["items"]] if batch else [int(path.split("/")[4])])
        if any(ident not in state["rows"] for ident in ids):
            return httpx.Response(200, json={"code": 404, "message": "目标不存在"})
        intent = body if preparing and not batch else body["intent"]
        plans = [plan(ident, intent) for ident in sorted(ids)]
        if preparing:
            return httpx.Response(200, json={"code": 200, "data": plans if batch else plans[0]})
        expected = {item["id"]: item["version"] for item in body["items"]} if batch else {
            ids[0]: body["version"]}
        if any(row["version"] != expected[row["id"]] for row in plans):
            return httpx.Response(200, json={"code": 409, "message": "版本已变化，整批未执行"})
        results = apply(plans, ids)
        return httpx.Response(200, json={"code": 200, "data": results if batch else results[0]})

    with httpx.Client(transport=httpx.MockTransport(handle)) as client, \
            patch.object(base, "_client", client), \
            patch.object(confirm.settings, "confirmation_secret", SECRET):
        yield state
