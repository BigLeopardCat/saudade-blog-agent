"""Continue a verified body page without asking the model to copy version guards."""

import json
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from agent.domains.articles.evidence import ArticleEvidence
from agent.domains.articles.reading import project
from agent.products.blog.articles import ArticleMiddleware
from agent.runtime.session import ToolSession
from eval.fixtures.document_reads import RevisionChanged, article_page, document_page
from rag.reading import render_article_result
from rag.reading_requests import tool_args
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
    assert state.bind_read_args(args) == args


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
    assert state.bind_read_args(args) == args


def test_cursor_snapshot_is_stable_and_failures_invalidate_the_current_cursor():
    state, data = evidence()
    snapshot = state.snapshot()
    state.observe("get_article_detail", {"article_id": ARTICLE}, unavailable("changed"))
    args = {"article_id": ARTICLE, "offset": 20}
    assert state.bind_read_args(args) == args
    restored = ArticleEvidence.restore(json.loads(json.dumps(snapshot)))
    assert restored.bind_read_args(args) == {
        **args, "revision": data["contentRevision"], "section_schema": data["sectionSchema"]}
    session = SimpleNamespace(task={"id": "new"}, recovering=False, render_result=lambda *a: None)
    middleware = ArticleMiddleware(session)
    middleware.before_agent({"evidence": snapshot, "evidence_task_id": "old",
                             "proposal": {"pending": True}}, None)
    assert middleware.evidence.bind_read_args(args) == args


def test_budgeted_body_cursor_never_skips_text_not_shown_to_the_model():
    raw = article_page({"noteTitle": "Source", "noteContent": BODY * 500}, ARTICLE)
    text, visible = render_article_result(raw, str(raw), 1800)
    assert 0 < visible["nextOffset"] < raw["nextOffset"]
    state = ArticleEvidence()
    state.observe("get_article_detail", {"article_id": ARTICLE}, ok(text, data=visible))
    args = {"article_id": ARTICLE, "offset": visible["nextOffset"]}
    assert state.bind_read_args(args)["revision"] == raw["contentRevision"]
    unseen = {"article_id": ARTICLE, "offset": raw["nextOffset"]}
    assert state.bind_read_args(unseen) == unseen


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
                    "args": tool_args({"article_id": ARTICLE,
                             "view": "full_text" if position == 0 else "source_range",
                             "offset": position * 20, "max_chars": 20}),
                    "id": f"read-{position}"}])
            return AIMessage(content="The article changed." if changed else "Read the next page.")

    result = ToolSession(Model(), {}, {notes.get_article_detail.name: notes.get_article_detail},
                         {}, middleware=(ArticleMiddleware,)).run([HumanMessage("Read two pages")])
    assert result.reply and len(messages) == 3 and len(requests) == 2
    assert requests[1]["section_schema"] == evidence()[1]["sectionSchema"]
    assert requests[1]["revision"] == evidence()[1]["contentRevision"]
    results = [m for m in messages[-1] if getattr(m, "type", None) == "tool"]
    assert results[-1].artifact["kind"] == ("unavailable" if changed else "ok")


def test_visible_directory_binds_sections_and_its_exact_next_page():
    body = "".join(f"## Chapter {n}\nparagraph {n}\n" for n in range(30))
    page = project(document_page("note", ARTICLE, "Source", body, {"view": "outline"}),
                   "note", ARTICLE, "", 0, 24000, "", "", view="outline")
    state = ArticleEvidence()
    state.observe("get_article_detail", {"article_id": ARTICLE, "view": "outline"},
                  ok("outline", data=page))
    guards = {"revision": page["contentRevision"], "section_schema": page["sectionSchema"]}
    next_page = {"article_id": ARTICLE, "view": "outline", "offset": page["nextSectionsOffset"]}
    section = {"article_id": ARTICLE, "section": page["sections"][0]["id"]}
    assert state.bind_read_args(next_page) == {**next_page, **guards}
    assert state.bind_read_args(section) == {**section, **guards}
    assert state.documents == state.read_ranges == {}
    for unseen in ({**next_page, "offset": next_page["offset"] + 1},
                   {**section, "section": "s30"}, {**section, "offset": 1},
                   {**section, "doc_type": "talk"}, {**section, "revision": "0" * 64}):
        assert state.bind_read_args(unseen) == unseen
    restored = ArticleEvidence.restore(json.loads(json.dumps(state.snapshot())))
    assert restored.bind_read_args(next_page) == {**next_page, **guards}


def test_conflicting_directory_versions_do_not_guess_which_version_to_bind():
    state = ArticleEvidence()
    for body in ("## Chapter\nold body", "## Chapter\nnew body"):
        page = project(document_page("note", ARTICLE, "Source", body, {"view": "outline"}),
                   "note", ARTICLE, "", 0, 24000, "", "", view="outline")
        state.observe("get_article_detail", {"article_id": ARTICLE, "view": "outline"},
                      ok("outline", data=page))
    args = {"article_id": ARTICLE, "section": "s1"}
    assert state.bind_read_args(args) == args
    state.observe("get_article_detail", args, unavailable("source changed"))
    assert state.read_targets == []


def test_budgeted_directory_binds_only_the_cursor_and_headings_actually_shown():
    body = "".join(f"## Chapter {n}\nparagraph {n}\n" for n in range(30))
    raw = project(document_page("note", ARTICLE, "Source", body, {"view": "outline"}),
                  "note", ARTICLE, "", 0, 24000, "", "", view="outline")
    text, shown = render_article_result(raw, "outline", 750)
    assert 0 < len(shown["sections"]) < len(raw["sections"])
    state = ArticleEvidence()
    state.observe("get_article_detail", {"article_id": ARTICLE, "view": "outline"},
                  ok(text, data=shown))
    args = {"article_id": ARTICLE, "view": "outline", "offset": shown["nextSectionsOffset"]}
    assert state.bind_read_args(args)["revision"] == raw["contentRevision"]
    unshown = {**args, "offset": raw["nextSectionsOffset"]}
    assert state.bind_read_args(unshown) == unshown


def test_framework_binds_the_observed_directory_cursor_before_host_validation(monkeypatch):
    body = "".join(f"## Chapter {n}\nparagraph {n}\n" for n in range(30))
    requests = []

    def read(path, **kwargs):
        params = {key: values[0] for key, values in parse_qs(urlsplit(path).query).items()}
        requests.append(params)
        return document_page("note", ARTICLE, "Source", body, params)

    monkeypatch.setattr(notes, "_get", read)

    class Model:
        max_tokens = 128

        def bind_tools(self, *args, **kwargs):
            return self

        def invoke(self, messages, **kwargs):
            count = sum(getattr(m, "type", None) == "tool" for m in messages)
            if count < 2:
                return AIMessage(content="", tool_calls=[{"name": "get_article_detail",
                    "args": tool_args(
                        {"article_id": ARTICLE, "view": "outline", "offset": count * 12}),
                    "id": f"directory-{count}"}])
            return AIMessage(content="Directory pages are available.")

    result = ToolSession(Model(), {}, {notes.get_article_detail.name: notes.get_article_detail},
                         {}, middleware=(ArticleMiddleware,)).run(
                             [HumanMessage("Read the directory")])
    assert result.reply and len(requests) == 2
    assert requests[1]["offset"] == "12" and requests[1]["view"] == "outline"
    assert len(requests[1]["revision"]) == 64 and requests[1]["section_schema"]


def test_body_page_uses_the_host_limit_and_keeps_a_verified_continuation(monkeypatch):
    body = "source text " * 4000
    requests = []

    def read(path, **kwargs):
        params = {key: values[0] for key, values in parse_qs(urlsplit(path).query).items()}
        requests.append(params)
        return document_page("note", ARTICLE, "Source", body, params)

    monkeypatch.setattr(notes, "_get", read)
    first = notes.get_article_detail.invoke(tool_args({"article_id": ARTICLE, "view": "full_text"}))
    assert requests[0]["limit"] == "32768"
    assert first.data["noteContent"] == body[:32768] and first.data["nextOffset"] == 32768
    state = ArticleEvidence()
    state.observe("get_article_detail", {"article_id": ARTICLE}, first)
    second = notes.get_article_detail.invoke(tool_args(state.bind_read_args({"article_id": ARTICLE,
                                                                  "view": "source_range",
                                                                  "offset": 32768})))
    assert second.kind == "ok" and second.data["noteContent"] == body[32768:]
    assert second.data["readComplete"] and second.data["nextOffset"] is None


def test_selected_section_reports_that_complete_subtree_includes_children(monkeypatch):
    body = "## Parent\nparent body\n### Child\nchild body\n## Sibling\nsibling body\n"

    def read(path, **kwargs):
        params = {key: values[0] for key, values in parse_qs(urlsplit(path).query).items()}
        return document_page("note", ARTICLE, "Source", body, params)

    monkeypatch.setattr(notes, "_get", read)
    result = notes.get_article_detail.invoke(
        tool_args({"article_id": ARTICLE, "view": "source_range", "section": "Parent"}))
    assert result.kind == "ok" and result.data["selectedScope"] == "section_subtree"
    assert result.data["readComplete"] and "child body" in result.data["sectionText"]
    assert "sibling body" not in result.data["sectionText"]
    _, visible = render_article_result(result.data, result.text, 700)
    assert visible["selectedScope"] == "section_subtree"
