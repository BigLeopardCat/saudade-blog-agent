"""RAG candidates must lead to successful body reads before factual answers."""

import ast

from langchain_core.messages import AIMessage, HumanMessage

from agent.domains.articles.evidence import ArticleEvidence
from agent.products.blog.articles import ArticleMiddleware
from agent.runtime.session import ToolSession
from rag.reading import article_page, render_article_result
from tools.base import SITE_BASE, get_article_detail
from tools.result import empty, ok, unavailable


class Model:
    def __init__(self, replies):
        self.replies = iter(replies)
        self.prompts = []

    def bind_tools(self, tools, **kwargs):
        from _budget_model import schemas
        self.kwargs = {"tools": schemas(tools)}
        return self

    def invoke(self, prompt):
        self.prompts.append(list(prompt))
        return next(self.replies)


class Tool:
    def __init__(self, name, value):
        self.name, self.value = name, value

    def invoke(self, args, config=None):
        return self.value


def call(name, args, ident):
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": ident}])


def test_candidate_only_answer_stops_without_restarting_or_asserting_article_contents():
    model = Model(
        [
            call("rag_search", {"query": "topic"}, "s"),
            AIMessage(content="这篇文章给出的答案是 42。"),
        ]
    )
    tool = Tool("rag_search", ok("1. type=note id=1 score=1 title=Topic"))
    session = ToolSession(model, {}, {"rag_search": tool}, {}, middleware=(ArticleMiddleware,))
    result = session.run([HumanMessage(content="问文章")])
    assert result is not None and "无法核实文章正文" in result.reply


def test_empty_and_unavailable_searches_have_different_truthful_outcomes():
    for value, expected in [(empty("检索无结果"), "暂未"), (unavailable("outage"), "不可用")]:
        model = Model(
            [call("rag_search", {"query": "topic"}, "s"), AIMessage(content="不受支持的文章事实")]
        )
        result = ToolSession(model, {}, {"rag_search": Tool("rag_search", value)}, {},
                             middleware=(ArticleMiddleware,)).run([])
        assert expected in result.reply and "不受支持" not in result.reply


def test_failed_body_read_cannot_satisfy_evidence_contract():
    evidence = ArticleEvidence()
    evidence.observe("rag_search", {}, ok("candidate"))
    evidence.observe("get_article_detail", {"article_id": 1}, unavailable("outage"))
    assert evidence.issue("答案是42", SITE_BASE) == "article_body_missing"


def test_successful_body_read_adds_real_source_and_rejects_unread_articles():
    evidence = ArticleEvidence()
    evidence.observe("rag_search", {}, ok("candidate"))
    data = {"noteId": 1, "noteTitle": "Verified", "noteContent": "actual evidence"}
    evidence.observe("get_article_detail", {"article_id": 1}, ok(str(data), data=data))
    assert evidence.issue("回答", SITE_BASE) is None
    assert "/article/1" in evidence.sources("回答", SITE_BASE)
    assert evidence.issue(f"来源 {SITE_BASE}/article/99", SITE_BASE) == "unread_article_reference"
    assert evidence.issue("来源 https://untrusted.invalid/article/1", SITE_BASE)
    assert evidence.issue("来源 [伪造](/article/99)", SITE_BASE)


def test_render_budget_keeps_valid_data_and_cursor_at_visible_end():
    body = ("'\n\\" * 4000) + "TAIL"
    raw = article_page({"noteTitle": "T", "noteContent": body}, 1, max_chars=12000)
    text, visible = render_article_result(ok(str(raw), data=raw), 1600)
    assert len(text) <= 1600 and ast.literal_eval(text) == visible
    assert visible["noteContent"] == body[: visible["nextOffset"]]
    assert visible["nextOffset"] < raw["nextOffset"]
    assert visible["readComplete"] is False
    assert visible["source"]["end"] == visible["nextOffset"]


def test_real_tool_exposes_revision_bound_continuation_and_stable_section_ids(monkeypatch):
    import tools.base as base

    source = {"noteKey": 1, "noteTitle": "T", "noteContent": "## 重复\nfirst\n## 重复\nsecond"}
    monkeypatch.setattr(
        base, "_get", lambda path, **kwargs: {} if path.endswith("/stats") else source
    )
    first = get_article_detail.invoke({"article_id": 1, "max_chars": 8})
    assert first.kind == "ok" and first.data["nextOffset"] == 8
    second = get_article_detail.invoke(
        {"article_id": 1, "offset": 8, "revision": first.data["contentRevision"]}
    )
    assert first.data["noteContent"] + second.data["noteContent"] == source["noteContent"]
    selected = get_article_detail.invoke(
        {"article_id": 1, "section": "s2", "revision": first.data["contentRevision"]}
    )
    assert "second" in selected.data["sectionText"] and "first" not in selected.data["sectionText"]


def test_candidate_reference_validation_precedes_streamed_output():
    model = Model([
        call("search_notes", {"keyword": "topic"}, "search"),
        AIMessage(content="来源：[不存在的文章](/article/999)"),
    ])
    value = ok("候选文章19", data=[{"noteId": 19, "noteTitle": "真实候选"}])
    session = ToolSession(model, {}, {"search_notes": Tool("search_notes", value)}, {},
                          middleware=(ArticleMiddleware,))
    runner = session.stream([HumanMessage(content="找一篇文章")])
    visible = []
    while True:
        try:
            visible.append(next(runner).content)
        except StopIteration as stop:
            result = stop.value
            break
    assert "无法核实" in result.reply
    assert "/article/999" not in "".join(visible) + result.reply
    assert "".join(visible) == result.reply
