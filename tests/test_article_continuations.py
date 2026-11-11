"""Continue a verified body page without asking the model to copy version guards."""

import json
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from agent.domains.articles.evidence import ArticleEvidence
from agent.products.blog.articles import ArticleMiddleware
from agent.runtime.session import ToolSession
from eval.fixtures.document_reads import RevisionChanged, article_page, document_page
from rag.reading import render_article_result
from tools import notes
from tools.result import ok, unavailable

ARTICLE = 19
BODY = "A verified article body with enough text for several pages."


def evidence():
    data = article_page({"noteTitle": "Source", "noteContent": BODY}, ARTICLE, max_chars=20)
    state = ArticleEvidence()
    state.observe("get_article_detail", {"article_id": ARTICLE}, ok("body", data=data))
    return state, data


@pytest.mark.parametrize("change", [
    {"article_id": 20}, {"article_id": [19]}, {"article_id": True},
    {"doc_type": "talk"}, {"section": "s1"},
    {"view": "outline"}, {"offset": 21}, {"offset": True},
    {"revision": "0" * 64}, {"section_schema": "another-parser"},
])
def test_only_the_observed_next_body_page_can_supply_missing_guards(change):
    state, _ = evidence()
    args = {"article_id": ARTICLE, "offset": 20, **change}
    assert state.continuation_args(args) == args


@pytest.mark.parametrize("change", [
    {"source": None}, {"nextOffset": 21}, {"nextOffset": True},
    {"contentRevision": "0" * 64}, {"sectionSchema": "another-parser"},
    {"readComplete": True}, {"totalChars": 20}, {"noteContent": "different"},
    {"readOffset": False},
])
def test_malformed_or_complete_evidence_never_supplies_a_cursor(change):
    _, data = evidence()
    state = ArticleEvidence()
    state.observe("get_article_detail", {"article_id": ARTICLE},
                  ok("body", data={**data, **change}))
    args = {"article_id": ARTICLE, "offset": 20}
    assert state.continuation_args(args) == args


def test_cursor_snapshot_is_stable_and_failures_invalidate_the_current_cursor():
    state, data = evidence()
    snapshot = state.snapshot()
    state.observe("get_article_detail", {"article_id": ARTICLE}, unavailable("changed"))
    args = {"article_id": ARTICLE, "offset": 20}
    assert state.continuation_args(args) == args
    restored = ArticleEvidence.restore(json.loads(json.dumps(snapshot)))
    assert restored.continuation_args(args) == {
        **args, "revision": data["contentRevision"], "section_schema": data["sectionSchema"]}
    session = SimpleNamespace(task={"id": "new"}, recovering=False, render_result=lambda *a: None)
    middleware = ArticleMiddleware(session)
    middleware.before_agent({"evidence": snapshot, "evidence_task_id": "old",
                             "proposal": {"pending": True}}, None)
    assert middleware.evidence.continuation_args(args) == args


def test_budgeted_body_cursor_never_skips_text_not_shown_to_the_model():
    raw = article_page({"noteTitle": "Source", "noteContent": BODY * 500}, ARTICLE)
    text, visible = render_article_result(raw, str(raw), 1800)
    assert 0 < visible["nextOffset"] < raw["nextOffset"]
    state = ArticleEvidence()
    state.observe("get_article_detail", {"article_id": ARTICLE}, ok(text, data=visible))
    args = {"article_id": ARTICLE, "offset": visible["nextOffset"]}
    assert state.continuation_args(args)["revision"] == raw["contentRevision"]
    unseen = {"article_id": ARTICLE, "offset": raw["nextOffset"]}
    assert state.continuation_args(unseen) == unseen


@pytest.mark.parametrize("changed", [False, True])
def test_framework_binds_next_page_but_host_still_rejects_changed_body(monkeypatch, changed):
    requests, messages = [], []

    def read(path, **kwargs):
        params = {key: values[0] for key, values in parse_qs(urlsplit(path).query).items()}
        requests.append(params)
        body = BODY + " changed" if changed and len(requests) > 1 else BODY
        try:
            return document_page("note", ARTICLE, "Source", body, params)
        except RevisionChanged:
            return unavailable("Document changed", meta={"status": 409})

    monkeypatch.setattr(notes, "_get", read)
    monkeypatch.setattr("rag.search.invalidate_document", lambda *args: None)

    class Model:
        max_tokens = 128

        def bind_tools(self, *args, **kwargs):
            return self

        def invoke(self, prompt, **kwargs):
            messages.append(prompt)
            position = len(messages) - 1
            if position < 2:
                return AIMessage(content="", tool_calls=[{"name": "get_article_detail",
                    "args": {"article_id": ARTICLE, "offset": position * 20, "max_chars": 20},
                    "id": f"read-{position}"}])
            return AIMessage(content="The article changed." if changed else "Read the next page.")

    result = ToolSession(Model(), {}, {notes.get_article_detail.name: notes.get_article_detail},
                         {}, middleware=(ArticleMiddleware,)).run([HumanMessage("Read two pages")])
    assert result.reply and len(messages) == 3 and len(requests) == 2
    assert requests[1]["section_schema"] == evidence()[1]["sectionSchema"]
    assert requests[1]["revision"] == evidence()[1]["contentRevision"]
    results = [m for m in messages[-1] if getattr(m, "type", None) == "tool"]
    assert results[-1].artifact["kind"] == ("unavailable" if changed else "ok")
