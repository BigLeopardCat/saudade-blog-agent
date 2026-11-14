"""Article provenance checks preserve independent evidence and scoped failures."""

import json

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from agent.domains.articles.evidence import ArticleEvidence
from agent.domains.articles.requests import tool_args
from agent.products.blog.articles import ArticleMiddleware
from agent.runtime.session import ToolSession
from eval.fixtures.document_reads import article_page
from rag.reading import render_article_result
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


@pytest.mark.parametrize("value,reply", [
    (empty("检索无结果"), "暂未检索到相关文章，没有读取正文。"),
    (unavailable("outage"), "文章检索暂时不可用，没有读取正文。"),
])
def test_failed_search_preserves_qualified_answer_and_tool_evidence(value, reply):
    model = Model([call("rag_search", {"query": "topic"}, "s"), AIMessage(content=reply)])
    result = ToolSession(model, {}, {"rag_search": Tool("rag_search", value)}, {},
                         middleware=(ArticleMiddleware,)).run([])
    assert result.reply == reply and len(model.prompts) == 2
    observed = model.prompts[-1][-1]
    assert observed.content == str(value) and observed.artifact["kind"] == value.kind


@pytest.mark.parametrize("value", [empty("检索无结果"), unavailable("outage")])
def test_failed_search_cannot_authorize_an_invented_article_reference(value):
    model = Model([call("rag_search", {"query": "topic"}, "s"),
                   AIMessage(content="不受支持的文章事实 [来源](/article/99)")])
    result = ToolSession(model, {}, {"rag_search": Tool("rag_search", value)}, {},
                         middleware=(ArticleMiddleware,)).run([])
    assert "引用无法核实" in result.reply and "/article/99" not in result.reply


def test_auxiliary_search_failure_does_not_discard_a_successful_site_guide():
    from agent.principal import SOURCE_ASSERTION, Principal
    from tools.site import get_site_map

    reply = "站点提供留言板，当前账号不能导航到后台。文章检索失败，本次没有读取文章正文。"
    model = Model([call("rag_search", {"query": "guide"}, "search"),
                   call("get_site_map", {}, "guide"), AIMessage(content=reply)])
    cfg = {"configurable": {"principal": Principal(7, "user", SOURCE_ASSERTION), "user_id": 7}}
    session = ToolSession(model, cfg, {"rag_search": Tool("rag_search", unavailable("outage")),
                          "get_site_map": get_site_map}, {}, middleware=(ArticleMiddleware,))
    result = session.run([HumanMessage(content="查站点指南，区分我能打开的页面。")])
    assert result.reply == reply and len(model.prompts) == 3
    guide = next(message for message in model.prompts[-1] if message.type == "tool"
                 and message.name == "get_site_map")
    assert guide.artifact["kind"] == "ok"
    assert next(page for page in guide.artifact["data"]["pages"] if page["path"] == "/dashboard")[
        "navigationAvailable"] is False


def test_failed_body_read_cannot_satisfy_evidence_contract():
    evidence = ArticleEvidence()
    evidence.observe("rag_search", {}, ok("candidate"))
    evidence.observe("get_article_detail", {"article_id": 1}, unavailable("outage"))
    assert evidence.issue("答案是42", SITE_BASE) == "article_body_missing"


def test_successful_body_read_permits_real_source_and_rejects_unread_articles():
    evidence = ArticleEvidence()
    evidence.observe("rag_search", {}, ok("candidate"))
    data = {"noteId": 1, "noteTitle": "Verified", "noteContent": "actual evidence"}
    evidence.observe("get_article_detail", {"article_id": 1}, ok(str(data), data=data))
    assert evidence.issue("回答", SITE_BASE) is None
    assert evidence.issue(f"来源 {SITE_BASE}/article/1", SITE_BASE) is None
    assert evidence.issue(f"来源 {SITE_BASE}/article/99", SITE_BASE) == "unread_article_reference"
    assert evidence.issue("来源 https://untrusted.invalid/article/1", SITE_BASE)
    assert evidence.issue("来源 [伪造](/article/99)", SITE_BASE)


def test_read_documents_are_not_mechanically_appended_as_answer_sources():
    reply = "图片正在后台生成，完成后再展示。"
    model = Model([call("get_article_detail", {"article_id": 1}, "read"), AIMessage(content=reply)])
    data = {"noteId": 1, "noteTitle": "Architecture", "noteContent": "Actual evidence"}
    session = ToolSession(model, {}, {"get_article_detail": Tool("get_article_detail",
        ok("Actual evidence", data=data))}, {}, middleware=(ArticleMiddleware,))
    assert session.run([]).reply == reply


def test_render_budget_keeps_valid_data_and_cursor_at_visible_end():
    body = ("'\n\\" * 4000) + "TAIL"
    raw = article_page({"noteTitle": "T", "noteContent": body}, 1, max_chars=12000)
    text, visible = render_article_result(raw, str(raw), 1600)
    assert len(text) <= 1600 and json.loads(text) == visible
    assert visible["noteContent"] == body[: visible["nextOffset"]]
    assert visible["nextOffset"] < raw["nextOffset"]
    assert visible["readComplete"] is False
    assert visible["source"]["end"] == visible["nextOffset"]


def test_real_tool_exposes_revision_bound_continuation_and_stable_section_ids(monkeypatch):
    import tools.base as base

    source = {"noteKey": 1, "noteTitle": "T", "noteContent": "## 重复\nfirst\n## 重复\nsecond"}
    from urllib.parse import parse_qs, urlsplit

    from eval.fixtures.document_reads import document_page

    def read(path, **kwargs):
        query = {key: values[0] for key, values in parse_qs(urlsplit(path).query).items()}
        return document_page("note", 1, source["noteTitle"], source["noteContent"], query)

    monkeypatch.setattr(base, "_get", read)
    first = get_article_detail.invoke(tool_args(
        {"article_id": 1, "view": "full_text", "max_chars": 8}))
    assert first.kind == "ok" and first.data["nextOffset"] == 8
    second = get_article_detail.invoke(
        tool_args({"article_id": 1, "view": "source_range", "offset": 8,
         "revision": first.data["contentRevision"],
         "section_schema": first.data["sectionSchema"]})
    )
    assert first.data["noteContent"] + second.data["noteContent"] == source["noteContent"]
    selected = get_article_detail.invoke(
        tool_args({"article_id": 1, "view": "source_range", "section": "s2",
         "revision": first.data["contentRevision"],
         "section_schema": first.data["sectionSchema"]})
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
