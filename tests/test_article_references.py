"""A visible outbound link is a reference, never evidence of the linked body."""

import pytest
from langchain_core.messages import AIMessageChunk, HumanMessage
from langchain_core.tools import tool

from agent.domains.articles.evidence import ArticleEvidence
from agent.products.blog.articles import ArticleMiddleware
from agent.runtime.results import ReplyReset
from agent.runtime.session import ToolSession
from eval.fixtures.document_reads import article_page
from rag.reading import render_article_result
from tools.base import SITE_BASE
from tools.result import ok, unavailable


def observed(body):
    evidence = ArticleEvidence()
    data = {"noteTitle": "Source", "noteContent": body}
    evidence.observe("get_article_detail", {"article_id": 1}, ok(str(data), data=data))
    return evidence


@pytest.mark.parametrize("link", ["/article/2", SITE_BASE + "/article/2?section=3#part"])
def test_visible_same_site_reference_is_allowed_without_claiming_a_body_read(link):
    evidence = observed(f"Original body. See [Related]({link}).")
    reply = f"已读文章提到 [Related]({link})，未读取它的正文。"
    assert evidence.issue(reply, SITE_BASE) is None
    assert evidence.documents == {1: "Source"}
    assert "/article/1" in evidence.sources(reply, SITE_BASE)
    assert evidence.sources(reply, SITE_BASE).count(link) == 1


def test_invented_and_foreign_links_remain_rejected_even_when_body_has_other_references():
    evidence = observed("[Related](/article/2) and https://foreign.invalid/article/3")
    for reply in ("[invented](/article/99)", "[foreign-derived](/article/3)",
                  "https://foreign.invalid/article/2"):
        assert evidence.issue(reply, SITE_BASE) == "unread_article_reference"


def test_unseen_or_failed_read_cannot_authorize_a_reference():
    raw = article_page({"noteTitle": "Source", "noteContent": "x" * 4000
                        + "[Unseen](/article/9)"}, 1)
    text, visible = render_article_result(raw, str(raw), 1200)
    evidence = ArticleEvidence()
    evidence.observe("get_article_detail", {"article_id": 1}, ok(text, data=visible))
    evidence.observe("get_article_detail", {"article_id": 2}, unavailable("outage", data={
        "noteContent": "[Failed](/article/10)"}))
    assert evidence.issue("[Unseen](/article/9)", SITE_BASE) == "unread_article_reference"
    assert evidence.issue("[Failed](/article/10)", SITE_BASE) == "unread_article_reference"


def test_missing_read_sources_are_appended_even_if_reply_already_links_a_different_article():
    evidence = observed("Original body.")
    data = {"noteTitle": "Second", "noteContent": "Other body."}
    evidence.observe("get_article_detail", {"article_id": 2}, ok(str(data), data=data))
    reply = "[Source](/article/1)"
    rendered = evidence.sources(reply, SITE_BASE)
    assert rendered.count("/article/1") == 1 and rendered.count("/article/2") == 1
    assert evidence.sources(rendered, SITE_BASE) == rendered


@pytest.mark.parametrize("reference,allowed,finish", [
    ("/article/2", True, "stop"), ("/article/99", False, "stop"),
    ("/article/2", False, "length"),
])
def test_direct_article_answer_is_validated_before_its_chunks_become_visible(
    reference, allowed, finish
):
    @tool
    def get_article_detail(article_id: int) -> object:
        """Read a fixture article."""
        data = {"noteTitle": "Source", "noteContent": "Body. [Related](/article/2)"}
        return ok(str(data), data=data)

    class Model:
        max_tokens = 64

        def __init__(self):
            self.round = 0

        def bind_tools(self, *args, **kwargs):
            return self

        def stream(self, prompt, **kwargs):
            self.round += 1
            if self.round == 1:
                yield AIMessageChunk(content="", tool_calls=[{
                    "name": "get_article_detail", "args": {"article_id": 1}, "id": "read"}])
            else:
                yield AIMessageChunk(content=f"已读文章引用 [Related]({reference})。",
                                     response_metadata={"finish_reason": finish})
                assert visible == [], "article answer leaked before source validation"

    visible = []
    session = ToolSession(Model(), {}, {get_article_detail.name: get_article_detail}, {},
                          middleware=(ArticleMiddleware,))
    runner = session.stream([HumanMessage(content="概括文章")])
    while True:
        try:
            item = next(runner)
            if isinstance(item, ReplyReset):
                visible.clear()
            else:
                visible.append(item.content)
        except StopIteration as stop:
            result = stop.value
            break
    assert "".join(visible) == result.reply
    if allowed:
        assert reference in result.reply and "/article/1" in result.reply
    elif finish == "length":
        assert reference not in result.reply and "尚未完整生成" in result.reply
    else:
        assert reference not in result.reply and "无法核实" in result.reply


@pytest.mark.parametrize("tool", ["list_notes", "get_top_notes", "list_admin_notes"])
def test_catalog_candidates_remain_reference_evidence_after_an_empty_search(tool):
    from tools.result import empty

    evidence = ArticleEvidence()
    evidence.observe("rag_search", {}, empty("no matches"))
    evidence.observe(tool, {}, ok("a visible article", data={
        "resource": "articles", "items": [{"id": 19, "title": "Candidate"}]}))
    assert evidence.issue("[Candidate](/article/19)", SITE_BASE) is None
    assert evidence.issue("[Invented](/article/99)", SITE_BASE) == "unread_article_reference"
    assert evidence.documents == {} and evidence.provenance(SITE_BASE)["body_reads"] == []


def test_catalog_facts_from_wrong_resources_or_failed_reads_cannot_justify_article_links():
    from tools.result import empty

    evidence = ArticleEvidence()
    evidence.observe("rag_search", {}, empty("no matches"))
    evidence.observe("list_notes", {}, ok("category", data={
        "resource": "categories", "items": [{"id": 19, "title": "Not an article"}]}))
    evidence.observe("list_notes", {}, unavailable("failed", data={
        "resource": "articles", "items": [{"id": 20, "title": "Failed article"}]}))
    assert not evidence.candidates
    for ident in (19, 20):
        assert (
            evidence.issue(f"[Unknown](/article/{ident})", SITE_BASE) == "unread_article_reference"
        )


def test_runtime_keeps_a_verified_catalog_recommendation_after_search_has_no_matches():
    from _memory_support import Capture

    from tools.result import empty

    @tool
    def rag_search() -> object:
        """Search a synthetic article corpus."""
        return empty("no matching articles")

    @tool
    def get_top_notes() -> object:
        """Read a verified synthetic article catalog."""
        return ok("Candidate article #19", data={"resource": "articles", "items": [
            {"id": 19, "title": "Candidate"}]})

    model = Capture(AIMessageChunk(content="", tool_calls=[{
        "name": "rag_search", "args": {}, "id": "search"}]),
        AIMessageChunk(content="", tool_calls=[{
            "name": "get_top_notes", "args": {}, "id": "catalog"}]),
        AIMessageChunk(content="推荐 [Candidate](/article/19)。"))
    session = ToolSession(model, {}, {t.name: t for t in (rag_search, get_top_notes)}, {},
                          middleware=(ArticleMiddleware,))
    result = session.run([HumanMessage(content="推荐一篇文章")])
    assert result.reply == "推荐 [Candidate](/article/19)。"
    assert len(model.prompts) == 3
