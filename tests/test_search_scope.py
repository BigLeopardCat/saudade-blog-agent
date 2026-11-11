"""Search evidence carries query scope and cannot turn missing service data into absence."""

import json

import httpx
import pytest
from _framework_fixture import call, model_replies
from langchain_core.messages import HumanMessage

from agent.products.blog.articles import ArticleMiddleware
from agent.runtime.session import ToolSession
from tools import base
from tools.notes import search_notes


@pytest.mark.parametrize("data", [None, {}, {"items": []}, [None], [{"noteKey": True}]])
def test_unknown_search_shapes_are_not_empty_matches(monkeypatch, data):
    monkeypatch.setattr(base, "_get", lambda *a, **kw: data)
    result = search_notes.invoke({"keyword": "cake"})
    assert result.kind == "unavailable" and result.data is None


def test_empty_matches_preserve_the_actual_query_and_public_scope(monkeypatch):
    monkeypatch.setattr(base, "_get", lambda *a, **kw: [])
    result = search_notes.invoke({"keyword": "巧克力蛋糕"})
    assert result.kind == "empty"
    assert json.loads(str(result)) == result.data == {
        "resource": "articles", "scope": "public_articles_keyword_matches",
        "query": "巧克力蛋糕",
        "items": [], "returnedCount": 0,
        "resultSummary": "本次查询在当前检索范围内未命中；相关内容是否存在仍未知。"}


def test_blank_search_does_not_turn_into_an_unbounded_catalog_read(monkeypatch):
    monkeypatch.setattr(base, "_get", lambda *a, **kw: pytest.fail("unexpected request"))
    assert search_notes.invoke({"keyword": "  "}).kind == "invalid_args"


def test_negative_search_acceptance_requires_scoped_evidence():
    from eval import native_assertions

    gold = {"require_observations": [{"tools": ["search_notes"], "kinds": ["empty"],
        "result_by_tool": {"search_notes": {
            "resource": "articles", "scope": "public_articles_keyword_matches"}}}]}
    for body, valid in [("[]", False), (json.dumps({"resource": "articles",
            "scope": "public_articles_keyword_matches", "items": []}), True)]:
        result = {"native_events": [{"node": "native", "event": "call", "name": "search_notes",
                   "kind": "empty", "result": body}]}
        assert (not native_assertions.check(gold, result)) is valid


def test_scoped_metadata_reaches_the_actual_model_and_article_evidence(monkeypatch):
    reads = []

    def handle(request):
        reads.append(request)
        assert request.url.path.endswith("/notes/search")
        return httpx.Response(200, json={"code": 200, "data": [{
            "noteKey": 19, "noteTitle": "真实候选", "noteContent": "private-body-canary",
            "noteTagRefs": [{"id": 7, "level": 1, "name": "标签"}]}]})

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        monkeypatch.setattr(base, "_client", client)
        with model_replies({"tool_calls": [call("search_notes", "search", keyword="候选")]},
                           {"content": "找到《真实候选》。"}) as (model, requests):
            session = ToolSession(model, {}, {"search_notes": search_notes}, {},
                                  middleware=(ArticleMiddleware,))
            result = session.run([HumanMessage(content="查找候选文章")])
    messages = [m for m in requests[-1]["messages"] if m["role"] == "tool"]
    content = json.loads(messages[-1]["content"])
    assert content["query"] == "候选" and content["returnedCount"] == 1
    assert "resultSummary" not in content
    assert "searchedFields" not in content
    assert content["items"][0]["tags"] == [{"id": 7, "level": 1, "name": "标签"}]
    assert "private-body-canary" not in str(requests)
    assert result.reply == "找到《真实候选》。" and len(reads) == 1
