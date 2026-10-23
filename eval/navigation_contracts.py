"""Task outcomes for recommendations and navigation, without a real browser or API."""

import json
import re
from contextlib import contextmanager
from unittest.mock import patch

import httpx

from agent import confirm
from eval.own_fixtures import SECRET
from rag import search
from tools import base
from utils.trace_io import load_trace


@contextmanager
def backend():
    state = {"requests": [], "unsupported_reads": [], "articles": [
        {"id": 719, "title": "Git 分支实践", "status": "public", "isPublic": True,
         "isTop": 1, "categoryId": 11, "categoryName": "编程", "tags": []},
        {"id": 726, "title": "Python 工具开发", "status": "public", "isPublic": True,
         "isTop": 1, "categoryId": 11, "categoryName": "编程", "tags": []},
    ]}

    docs = [{"type": "note", "id": row["id"], "title": row["title"],
             "content": "这是一篇编程实践文章，介绍 " + row["title"] + "。"}
            for row in state["articles"]]
    index = search.RagIndex(corpus_loader=lambda: docs, vector_warmer=lambda _: None)
    index.build()

    def handle(request):
        state["requests"].append(request)
        if request.method == "GET" and request.url.path.endswith("/articles"):
            page = int(request.url.params.get("page", "1"))
            size = int(request.url.params.get("page_size", "5"))
            rows = state["articles"]
            return httpx.Response(200, json={"code": 200, "total": len(rows),
                                            "data": rows[(page - 1)*size:page*size]})
        if request.method == "POST" and request.url.path.endswith("/notes/search"):
            keyword = json.loads(request.content).get("keyword", "").casefold()
            rows = [{"noteKey": doc["id"], "noteTitle": doc["title"], "status": "public",
                     "isTop": 1, "noteTags": ""} for doc in docs
                    if keyword and keyword in (doc["title"] + doc["content"]).casefold()]
            return httpx.Response(200, json={"code": 200, "data": rows})
        if request.method == "GET" and (match := re.search(r"/notes/(\d+)(/stats)?$",
                                                           request.url.path)):
            doc = next((doc for doc in docs if doc["id"] == int(match[1])), None)
            if doc:
                data = ({"views": 10, "likes": 1, "favorites": 0} if match[2] else {
                    "noteKey": doc["id"], "noteTitle": doc["title"],
                    "noteContent": doc["content"], "status": "public", "noteTags": ""})
                return httpx.Response(200, json={"code": 200, "data": data})
        if request.method == "GET" and not re.search(r"/notes/\d+(?:/stats)?$",
                                                     request.url.path):
            state["unsupported_reads"].append(request.url.path)
        return httpx.Response(404)

    with httpx.Client(transport=httpx.MockTransport(handle)) as client, \
            patch.object(base, "_client", client), \
            patch.object(confirm.settings, "jwt_secret", SECRET), \
            patch.object(search, "_index", index), \
            patch.object(search, "is_enabled", lambda: False):
        yield state


def _require(condition, message):
    if not condition:
        raise ValueError(message)


def _links(result, state):
    urls = set(re.findall(r"https?://[^\s)<>\"，。]+/article/\d+", result["text"]))
    known = {base.SITE_BASE.rstrip("/") + f"/article/{row['id']}"
             for row in state["articles"]}
    _require(len(urls) == 1 and urls <= known, "recommendation lacks one sourced article link")
    trace = load_trace(result["trace"])
    sources = {"get_top_notes", "list_notes", "rag_search", "search_notes", "get_article_detail"}
    _require(trace and any(event.get("event") == "call" and event.get("kind") == "ok"
                           and event.get("name") in sources for event in trace["events"]),
             "recommendation did not read a synthetic article source")
    return urls.pop()


def _navigation(result, url):
    commands = [row["cmd"] for row in result["exec_rows"] if isinstance(row.get("cmd"), dict)]
    _require(commands == [{"kind": "navigate", "url": url, "mode": "direct"}]
             and len(result["commands"]) == 1,
             "expected exactly one navigation to the selected article")
    _require(not result["confirm_tokens"], "reversible navigation asked for confirmation")


def recommend_only(turn, state):
    result = turn("推荐一篇置顶文章，只给我一条文章链接，先不要打开。")
    url = _links(result, state)
    _require(not result["commands"] and not result["exec_rows"]
             and not result["confirm_tokens"], "recommendation unexpectedly performed an action")
    return url


def followup(turn, state):
    url = recommend_only(turn, state)
    _navigation(turn("带我过去呀"), url)


def recommend_and_open(turn, state):
    result = turn("猫咪推荐一篇文章带我过去")
    _navigation(result, _links(result, state))


def direct(turn, state):
    url = base.SITE_BASE.rstrip("/") + "/article/719"
    _navigation(turn("带我去这篇文章：" + url), url)


CASES = [("navigation_only_recommend", backend, recommend_only),
         ("navigation_followup", backend, followup),
         ("navigation_combined", backend, recommend_and_open),
         ("navigation_direct", backend, direct)]
