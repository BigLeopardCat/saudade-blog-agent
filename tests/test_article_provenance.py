"""A known article link and a visible body range are separate kinds of evidence."""

import ast
import json

from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.tools import tool

from agent.domains.articles.evidence import ArticleEvidence
from agent.products.blog import articles
from agent.runtime.session import ToolSession
from eval.online_judge import runtime_context
from rag.reading import article_page, render_article_result
from tools.base import SITE_BASE
from tools.result import ok, unavailable
from utils.trace import tool_result_text


def observe(evidence, data, limit=20000):
    text, visible = render_article_result(data, str(data), limit)
    evidence.observe("get_article_detail", {"article_id": data["noteKey"]},
                     ok(text, data=visible))
    return visible


def test_candidates_and_mentioned_links_do_not_become_body_read_evidence():
    evidence = ArticleEvidence()
    evidence.observe("rag_search", {}, ok("Candidate", data=[
        {"id": 19, "title": "Candidate", "type": "note"}]))
    assert evidence.issue("[Candidate](/article/19)", SITE_BASE) is None
    assert evidence.provenance(SITE_BASE) == {
        "candidate_articles": [{"id": 19, "title": "Candidate"}],
        "body_reads": [], "mentioned_article_ids": []}
    observe(evidence, article_page({"noteTitle": "Source", "noteContent":
                                    "Read this. [Related](/article/20)"}, 1))
    assert evidence.provenance(SITE_BASE)["mentioned_article_ids"] == [20]
    assert [row["id"] for row in evidence.provenance(SITE_BASE)["body_reads"]] == [1]


def test_read_range_uses_the_visible_excerpt_not_the_full_backend_result():
    evidence = ArticleEvidence()
    raw = article_page({"noteTitle": "Source", "noteContent": "正文" * 5000}, 19)
    visible = observe(evidence, raw, limit=1800)
    observed = evidence.provenance(SITE_BASE)["body_reads"][0]["ranges"][0]
    assert observed == {
        "revision": raw["contentRevision"], "section_id": "", "start": 0,
        "end": visible["nextOffset"], "visible_chars": len(visible["noteContent"]),
        "selected_total_chars": 10000, "selected_range_reaches_end": False}
    assert observed["end"] < raw["source"]["end"]


def test_article_trace_preserves_a_complete_model_observation_above_the_old_log_limit():
    source = "Article body. " * 1713 + "TAIL_CANARY"
    data = article_page({"noteTitle": "Source", "noteContent": source}, 19, max_chars=24000)
    text, visible = render_article_result(data, str(data), 28000)
    recorded = ast.literal_eval(tool_result_text(text, "get_article_detail"))
    assert recorded["noteContent"] == visible["noteContent"] == source
    assert recorded["source"]["end"] == len(source)


def test_section_end_and_cross_revision_reads_do_not_claim_whole_article_coverage():
    evidence = ArticleEvidence()
    source = {"noteTitle": "Source", "noteContent": "## First\none\n## Second\ntwo"}
    first = article_page(source, 1, section="First")
    observe(evidence, first)
    before = evidence.snapshot()
    observe(evidence, article_page({**source, "noteContent": source["noteContent"] + " changed"},
                                   1, section="Second"))
    ranges = evidence.provenance(SITE_BASE)["body_reads"][0]["ranges"]
    assert len(ranges) == 2 and all(row["selected_range_reaches_end"] for row in ranges)
    assert all(row["section_id"] for row in ranges)
    assert len({row["revision"] for row in ranges}) == 2
    assert len(before["read_ranges"][1]) == 1
    restored = ArticleEvidence.restore(json.loads(json.dumps(evidence.snapshot())))
    assert restored.provenance(SITE_BASE) == evidence.provenance(SITE_BASE)


def test_unlocated_body_and_failed_read_never_invent_revision_or_range():
    evidence = ArticleEvidence()
    data = {"noteTitle": "Legacy", "noteContent": "visible body"}
    evidence.observe("get_article_detail", {"article_id": 1}, ok(str(data), data=data))
    evidence.observe("get_article_detail", {"article_id": 2}, unavailable("failed", data=data))
    reads = evidence.provenance(SITE_BASE)["body_reads"]
    assert len(reads) == 1 and reads[0]["id"] == 1
    assert reads[0]["ranges"] == [{"revision": None, "section_id": None,
        "start": None, "end": None, "visible_chars": 12,
        "selected_total_chars": None, "selected_range_reaches_end": None}]


def test_judge_uses_only_recorded_article_provenance_without_backfilling_old_traces():
    first = {"candidate_articles": [{"id": 19, "title": "Candidate"}], "body_reads": []}
    latest = {"candidate_articles": [], "body_reads": [{"id": 19, "ranges": []}]}
    events = [{"node": "native", "event": "article_evidence", "provenance": row}
              for row in (first, latest)]
    assert runtime_context(events)["article_provenance"] == latest
    assert runtime_context([])["article_provenance"] is None


def test_actual_tool_result_provenance_reaches_semantic_context(monkeypatch):
    events = []
    monkeypatch.setattr(articles, "record", lambda node, event, **data:
                        events.append({"node": node, "event": event, **data}))

    @tool
    def get_article_detail(article_id: int) -> object:
        """Read an isolated article excerpt."""
        data = article_page({"noteTitle": "Source", "noteContent": "visible body"}, article_id,
                            max_chars=7)
        return ok(str(data), data=data)

    class Model:
        max_tokens = 128

        def __init__(self):
            self.responses = iter([AIMessage(content="", tool_calls=[{
                "name": "get_article_detail", "args": {"article_id": 19}, "id": "read"}]),
                AIMessage(content="本次只读取了开头片段。")])

        def bind_tools(self, *args, **kwargs):
            return self

        def invoke(self, messages, **kwargs):
            return next(self.responses)

    result = ToolSession(Model(), {}, {get_article_detail.name: get_article_detail}, {},
                         middleware=(articles.ArticleMiddleware,)).run([HumanMessage("Read")])
    provenance = runtime_context(events)["article_provenance"]
    assert "/article/19" in result.reply
    assert provenance["candidate_articles"] == []
    observed = provenance["body_reads"][0]["ranges"][0]
    assert observed["visible_chars"] == observed["end"] == 7
    assert observed["selected_range_reaches_end"] is False
