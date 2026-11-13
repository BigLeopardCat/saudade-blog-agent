"""Task outcomes for recommendations and navigation, without a real browser or API."""

import json
import re
from contextlib import contextmanager
from functools import partial
from unittest.mock import patch

import httpx

from agent import confirm
from eval.own_fixtures import SECRET
from rag import search
from rag.corpus import content_revision
from tools import base
from utils.trace_io import load_trace


@contextmanager
def backend(articles=None):
    state = {"requests": [], "unsupported_reads": [], "articles": [
        {"id": 719, "title": "Git 分支实践", "status": "public", "isPublic": True,
         "isTop": 1, "categoryId": 11, "categoryName": "编程", "tags": []},
        {"id": 726, "title": "Python 工具开发", "status": "public", "isPublic": True,
         "isTop": 1, "categoryId": 11, "categoryName": "编程", "tags": []},
    ]}

    if articles is not None:
        state["articles"] = articles
    docs = [{"type": "note", "id": row["id"], "title": row["title"],
             "content": "这是一篇编程实践文章，介绍 " + row["title"] + "。"}
            for row in state["articles"]]
    docs = [{**doc, "revision": content_revision(doc["title"], doc["content"])} for doc in docs]
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
        if request.method == "GET" and request.url.path.endswith("/categories"):
            return httpx.Response(200, json={"code": 200, "total": 1, "data": [
                {"id": 11, "name": "编程", "publicArticleCount": 2,
                 "color": None, "icon": None, "pathName": "编程"}]})
        if request.method == "POST" and request.url.path == "/api/public/documents/validate":
            refs = json.loads(request.content)["documents"]
            current = {doc["id"]: doc for doc in docs}
            return httpx.Response(200, json={"code": 200, "data": {
                "schemaVersion": 1, "documents": [
                    {"type": ref["type"], "id": ref["id"], "visible": ref["id"] in current,
                     "contentRevision": current[ref["id"]]["revision"]
                     if ref["id"] in current else None} for ref in refs]}})
        if request.method == "POST" and request.url.path.endswith("/notes/search"):
            keyword = json.loads(request.content).get("keyword", "").casefold()
            rows = [{"noteKey": doc["id"], "noteTitle": doc["title"], "status": "public",
                     "isTop": 1, "noteTags": ""} for doc in docs
                    if keyword and keyword in (doc["title"] + doc["content"]).casefold()]
            return httpx.Response(200, json={"code": 200, "data": rows})
        if request.method == "GET" and (match := re.search(
                r"/documents/note/(\d+)/read$", request.url.path)):
            from eval.fixtures.document_reads import document_page
            doc = next((doc for doc in docs if doc["id"] == int(match[1])), None)
            if doc:
                metadata = next(row for row in state["articles"] if row["id"] == doc["id"])
                data = document_page("note", doc["id"], doc["title"], doc["content"],
                                     request.url.params, metadata)
                return httpx.Response(200, json={"code": 200, "data": data})
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
            patch.object(confirm.settings, "confirmation_secret", SECRET), \
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


def feedback(turn, state, *, status="completed", path="/guestbook"):
    from langchain_core.messages import ToolMessage

    from agent.principal import SOURCE_ASSERTION, Principal
    from agent.runtime.checkpoint_conversations import CheckpointConversationStore
    from agent.runtime.service import AgentRuntime
    from eval import golden_evaluation as golden

    first = turn("请打开留言板。", browser_feedback=True, task_id="native_browser_task")
    commands = first.get("client_commands", [])
    _require(len(commands) == 1 and not first["confirm_tokens"],
             "browser mode did not deliver one unapproved reversible command")
    command = commands[0]
    _require(command["kind"] == "navigate" and command["path"] == "/guestbook",
             "browser command changed destination")
    runtime = golden.ensure_agent()
    config = {"configurable": {"user_id": 7, "conversation_id": 42,
        "thread_id": "conv_42", "principal": Principal(7, "user", SOURCE_ASSERTION)}}
    saved = runtime.get_state(config, subgraphs=True)
    _require(saved.interrupts and runtime.store.load(config).unfinished,
             "navigation did not wait in the framework checkpoint")
    restored = AgentRuntime(CheckpointConversationStore(runtime.saver),
                            application=runtime.application,
                            model_io=runtime.model_io, model_factory=runtime.model_factory)
    controls = {"resume_task": command["task_id"], "browser_feedback": True}
    with patch.object(golden, "ensure_agent", lambda: restored):
        waiting = turn("", **controls)
        _require(len(waiting["client_commands"]) == 1
                 and waiting["client_commands"][0]["command_id"] == command["command_id"]
                 and not waiting["tool_calls"], "missing ack changed or executed the action")
        ack = {"command_id": command["command_id"], "status": status, "path": path}
        foreign = turn("", **controls, browser_result={**ack, "command_id": "foreign"})
        _require(not foreign["client_commands"] and restored.store.load(config).unfinished,
                 "foreign browser result consumed the pending action")
        done = turn("", **controls, browser_result=ack)
        _require(done["text"].strip() and not done["client_commands"]
                 and not restored.store.load(config).unfinished, "ack did not finish saved task")
        facts = [json.loads(m.content) for m in restored.store.load(config).messages
                 if isinstance(m, ToolMessage) and m.name == "navigate_page"]
        success = status == "completed" and path == "/guestbook"
        _require(len(facts) == 1 and facts[0]["completion_confirmed"] is success,
                 "browser outcome was not reflected in the actual tool fact")
        replay = turn("", **controls, browser_result=ack)
        _require(not replay["client_commands"] and not replay["tool_calls"],
                 "duplicate acknowledgement repeated a browser action")
    rows = (waiting, foreign, replay)
    _require(all(isinstance(row["native_events"], list)
                 and not any(e.get("event") == "llm_start" for e in row["native_events"])
                 for row in rows), "control replay unexpectedly called the model")
    state["contract_evidence"] = {"checkpoint_reconstructed": True,
        "missing_foreign_duplicate_ack_model_calls": 0, "status": status,
        "observed_path": path, "completion_confirmed": success, "browser": "synthetic_ack"}


CASES = [("navigation_only_recommend", backend, recommend_only),
         ("navigation_followup", backend, followup),
         ("navigation_combined", backend, recommend_and_open),
         ("navigation_direct", backend, direct),
         ("navigation_feedback_complete", backend, feedback),
         ("navigation_feedback_failed", backend, partial(feedback, status="failed")),
         ("navigation_feedback_timeout", backend, partial(feedback, status="timeout")),
         ("navigation_feedback_wrong_path", backend, partial(feedback, path="/dashboard"))]
