"""Synthetic task contracts for post-discovery I/O efficiency and replay safety.

These cases use the existing runtime/trace driver and product tool catalog. Only
the batch-search fixture preloads its capability to isolate batching from discovery.
Scripted-model tests validate the contracts, not real-model task success rates.
"""

import json
import re
from collections import Counter
from contextlib import contextmanager
from unittest.mock import patch

import httpx

from agent import confirm
from agent.products.blog.application import BlogApplication
from agent.products.blog.capabilities import ToolCatalog
from eval import gallery_contracts, native_assertions
from eval.confirmation_failure_contracts import partial_backend
from eval.fixtures.document_reads import document_page
from rag import search
from rag.corpus import content_revision
from tools import base

ARTICLE_ID = 841
QUERY = "持久调度归还槽位"
FACT = "事务提交后保存执行结果，工作线程退出后归还并发槽位。"
WORDS = ["猫咪", "小猫", "喵"]
IMAGE_URL = "https://images.test/accepted.png"


def _require(value, message):
    if not value:
        raise ValueError(message)


def _check(result, **gold):
    failures = native_assertions.check(gold, result)
    _require(not failures, "; ".join(failures))
    if (maximum := gold.get("max_model_calls")) is not None:
        events = result["native_events"]
        attempts = sum(event.get("event") == "llm_start" for event in events)
        requests = sum(event.get("node") == "model" and event.get("event") == "wire_request"
                       for event in events)
        _require(not attempts or requests, "model HTTP request evidence is missing")
        _require(requests <= maximum, "model SDK HTTP requests exceeded the task call bound")


def _evidence(state, results):
    events = [event for result in results for event in result["native_events"]]
    state["contract_evidence"] = {
        "business_api": "synthetic",
        "scope": "runtime, tools and synthetic business HTTP; not database durability",
        "model_attempts": sum(event.get("event") == "llm_start" for event in events),
        "model_http_requests": sum(event.get("node") == "model" and
                                   event.get("event") == "wire_request" for event in events),
        "tool_requests": sum(event.get("event") == "tool_requested" for event in events),
        "business_http_requests": len(state["requests"]),
        "business_http_counts": dict(Counter(f"{request.method} {request.url.path}"
                                              for request in state["requests"])),
        "article_body_chars_returned": sum(state.get("body_reads", [])),
    }


@contextmanager
def article_backend():
    body = "## 背景\n" + "容量规划背景。" * 900 + "\n## 运行机制\n" + QUERY + "：" + FACT
    body += "\n" + "同一小节的其他背景信息。" * 900
    doc = {"type": "note", "id": ARTICLE_ID, "title": "合成调度材料", "content": body}
    doc["revision"] = content_revision(doc["title"], body)
    index = search.RagIndex(corpus_loader=lambda: [doc], vector_warmer=lambda _: None)
    index.build()
    state = {"requests": [], "body_reads": [], "unsupported_reads": [], "index": index}

    def handle(request):
        state["requests"].append(request)
        if request.method == "POST" and request.url.path == "/api/public/documents/validate":
            refs = json.loads(request.content)["documents"]
            data = {"schemaVersion": 1, "documents": [
                {"type": ref["type"], "id": ref["id"], "visible": ref["id"] == ARTICLE_ID,
                 "contentRevision": doc["revision"]} for ref in refs]}
        elif (request.method == "GET" and request.url.path
              == f"/api/public/documents/note/{ARTICLE_ID}/read"):
            data = document_page("note", ARTICLE_ID, doc["title"], body, request.url.params)
            state["body_reads"].append(len(data["content"]))
        else:
            state["unsupported_reads"].append(request.url.path)
            return httpx.Response(404)
        return httpx.Response(200, json={"code": 200, "data": data})

    with httpx.Client(transport=httpx.MockTransport(handle)) as client, \
            patch.object(base, "_client", client), patch.object(search, "_index", index), \
            patch.object(search, "is_enabled", lambda: False):
        yield state


def scoped_article(turn, state):
    result = turn(f"查文章{ARTICLE_ID}里关于“{QUERY}”的原始说明，只阅读命中段落后回答。")
    _evidence(state, [result])
    _require(FACT in result["text"], "requested original article fact was not delivered")
    _require(not result["confirm_tokens"] and not result["exec_rows"], "read caused a mutation")
    reads = [request for request in state["requests"] if request.method == "GET"]
    _require(len(reads) == 1, "article body was fetched repeatedly")
    _require(bool(reads[0].url.params.get("section")), "article locator became a whole-body read")
    _require(sum(state["body_reads"]) <= 2000, "article HTTP body exceeded 2000 characters")
    _check(result, max_model_calls=3, max_tool_calls={"rag_search": 1, "get_article_detail": 1},
           max_article_body_chars=2000, forbid_business_write=True,
           require_observations=[{"tools": ["rag_search"], "kinds": ["ok"],
                                  "args": {"article_id": ARTICLE_ID}},
                                 {"tools": ["get_article_detail"], "kinds": ["ok"]}])
    boundary = len(state["requests"])
    followup = turn("把刚读到的那句原始说明再发一次就好，不需要重新查询。")
    _evidence(state, [result, followup])
    _require(FACT in followup["text"], "already-read article fact was lost on followup")
    _require(len(state["requests"]) == boundary, "article followup repeated business reads")
    _check(followup, max_model_calls=1, max_tool_calls={"rag_search": 0,
                                                      "get_article_detail": 0},
           forbid_business_write=True)


@contextmanager
def activity_backend():
    from agent.domains.activity_search import search_public_activity

    state = {"requests": [], "unsupported_reads": [], "application": BlogApplication(
        ToolCatalog(read_tools={search_public_activity.name: search_public_activity}))}

    def handle(request):
        state["requests"].append(request)
        if request.method != "POST" or request.url.path != "/api/public/activity/search":
            state["unsupported_reads"].append(request.url.path)
            return httpx.Response(404)
        args = json.loads(request.content)
        words = args["keywords"]
        counts = {"announcements": 0, "board": 1, "talk": 0}
        data = {"keywords": words, "keywordMode": "any", "matchMode": "literal_case_insensitive",
                "sources": ["announcements", "board", "talk"], "counts": counts, "total": 1,
                "keywordCounts": [{"keyword": word, "counts": counts, "total": 1}
                                  for word in words],
                "page": args["page"], "pageSize": args["page_size"], "hasMore": False,
                "items": [{"id": 901, "source": "board", "title": "", "titleTruncated": False,
                           "content": "猫咪和小猫都在喵喵叫。", "contentTruncated": False,
                           "createdAt": None, "matchedKeywordIndices": list(range(len(words))),
                           "approved": 1, "displayName": "合成访客",
                           "displayNameTruncated": False}]}
        return httpx.Response(200, json={"code": 200, "data": data})

    with httpx.Client(transport=httpx.MockTransport(handle)) as client, \
            patch.object(base, "_client", client):
        yield state


def expanded_search(turn, state):
    result = turn("把猫咪、小猫、喵这几个词一起查一下公开动态，返回匹配的留言原文。")
    _evidence(state, [result])
    _require("猫咪和小猫都在喵喵叫。" in result["text"], "matching public content was omitted")
    _require(len(state["requests"]) == 1, "expanded keywords caused repeated business HTTP")
    _check(result, max_model_calls=2, max_tool_calls={"search_public_activity": 1},
           forbid_business_write=True, require_observations=[{
               "tools": ["search_public_activity"], "kinds": ["ok"],
               "args_list_contains": {"keywords": WORDS}}])


def gallery_reuse(turn, state):
    first = turn("只查图库图片72的元数据，告诉我文件名，不要分析画面。")
    boundary = len(state["requests"])
    second = turn("把刚刚那张图片直接展示出来，不要再查，也不要分析画面。")
    _evidence(state, [first, second])
    _require("学院风" in first["text"], "gallery metadata was not delivered")
    _require(re.search(r"!\[[^\]\n]*\]\(" + re.escape(IMAGE_URL) + r"\)", second["text"]),
             "followup did not display the already-read image")
    _require(boundary == 1 and len(state["requests"]) == boundary,
             "gallery followup fetched already-read metadata or scanned the catalog")
    _check(first, max_model_calls=3, max_tool_calls={"get_gallery_image_metadata": 1},
           forbid_business_write=True)
    _check(second, max_model_calls=1, max_tool_calls={"get_gallery_image_metadata": 0,
                                                   "get_gallery_index": 0},
           forbid_business_write=True)


def partial_write_replay(turn, state):
    prepared = turn("把我的未读通知标成已读。执行后核实编号101和102，只用JSON返回这两个编号到"
                    "isRead布尔值的映射，不要把失败的项目说成已完成。")
    _require(len(prepared["confirm_tokens"]) == 1, "missing approval for fixed notification scope")
    token = prepared["confirm_tokens"][0]
    grant = confirm.verify(token, 7, 42)
    _require(grant and grant["specs"] == [{"tool": "read_notifications",
                                         "args": {"ids": [101, 102], "all": False}}],
             "notification approval did not freeze the observed unread targets")
    _require(not any(request.method == "POST" for request in state["requests"]),
             "notification preparation wrote before approval")
    _check(prepared, max_model_calls=3, max_tool_calls={"prepare_notification_read": 1})
    done = turn("", token)
    before = len(state["requests"])
    replay = turn("", token)
    _evidence(state, [prepared, done, replay])
    writes = [request for request in state["requests"] if request.method == "POST"]
    _require(len(writes) == 1, "failed mutation or grant replay repeated the business write")
    _require(len(state["requests"]) == before, "grant replay repeated business I/O")
    _require(not done["exec_rows"] and not replay["exec_rows"],
             "unverified write produced a successful execution receipt")
    write_index = state["requests"].index(writes[0])
    readbacks = [request for request in state["requests"][write_index + 1:before]
                 if request.method == "GET" and request.url.path.endswith("/notifications")]
    _require(readbacks, "partial result was not read after failed verification")
    _check(done, max_model_calls=2, max_tool_calls={"list_notifications": 1},
           require_observations=[{"tools": ["list_notifications"], "kinds": ["ok"]}])
    actual = {str(row["id"]): row["isRead"] for row in state["items"] if row["id"] in (101, 102)}
    try:
        answer = json.loads(done["text"])
    except ValueError:
        answer = None
    _require(native_assertions.contains(answer, actual),
             "reported completion contradicts the independently observed partial business state")
    _check(replay, max_model_calls=0)


CASES = [("efficiency_scoped_article", article_backend, scoped_article),
         ("efficiency_expanded_search", activity_backend, expanded_search),
         ("efficiency_gallery_reuse", gallery_contracts.backend, gallery_reuse),
         ("efficiency_partial_write_replay", partial_backend, partial_write_replay)]
