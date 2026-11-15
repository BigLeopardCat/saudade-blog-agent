"""Require visible, versioned body evidence rather than a particular reader tool."""

import json
from copy import deepcopy

import pytest

from agent.domains.articles.evidence import ArticleEvidence
from eval.fixtures.document_reads import article_page
from eval.golden_evaluation import check_gold
from eval.online_baseline import load_suite
from rag.sections import PARSER_VERSION
from tools.result import ok


def result(mode):
    page = article_page({"noteTitle": "Source", "noteContent": "original body"}, 19)
    evidence = ArticleEvidence()
    if mode == "body":
        evidence.observe("get_article_detail", {"article_id": 19}, ok("read", data=page))
    elif mode in {"verified_rag", "index_only"}:
        row = {"type": "note", "id": 19, "title": "Source", "matches": [],
               "document_revision": page["contentRevision"], "section_schema": PARSER_VERSION,
               "passages": [{"source": page["source"], "text": page["noteContent"]}]}
        evidence.observe("rag_search", {}, ok("search", data=[row], meta={
            "body_evidence": {"verified": mode == "verified_rag"}}))
    else:
        evidence.observe("get_article_detail", {"article_id": 19}, ok("directory", data={
            "view": mode, "noteId": 19, "noteTitle": "Source", "source": None}))
    return {"text": "answer", "commands": [], "tool_calls": [], "exec_rows": [],
            "native_events": [{"node": "native", "event": "article_evidence",
                                                  "provenance": evidence.provenance("")}]}


@pytest.mark.parametrize("mode,valid", [("body", True), ("verified_rag", True),
                                        ("index_only", False), ("metadata", False),
                                        ("outline", False)])
def test_body_requirement_accepts_verified_ranges_and_rejects_catalog_only(mode, valid):
    assert (not check_gold({"require_body_reads": [{"article_id": 19}]}, result(mode))) is valid
    assert (not check_gold({"require_body_reads": [{}]}, result(mode))) is valid


def test_a_different_article_body_cannot_satisfy_the_named_source():
    assert check_gold({"require_body_reads": [{"article_id": 20}]}, result("body"))
    assert check_gold({"require_body_reads": [{"article_id": 19}, {"article_id": 20}]},
                      result("body"))


def summary_result():
    coverage = {"complete": True, "start": 0, "end": 12, "total_chars": 12,
                "revision": "a" * 64, "section_schema": PARSER_VERSION, "pages": 1}
    data = {"view": "summary", "noteId": 19, "noteTitle": "Source", "summary": "Derived",
            "strategy": "stuff", "segments": 1, "coverage": coverage}
    evidence = ArticleEvidence()
    evidence.observe("get_article_detail", {"article_id": 19, "selection": {"kind": "summary"}},
                     ok("summary", data=data))
    return {"text": "answer", "commands": [], "tool_calls": [], "exec_rows": [],
            "native_events": [
                {"node": "article_summary", "event": "source_page", "article_id": 19,
                 "offset": 0, "end": 12, "total_chars": 12, "revision": "a" * 64},
                {"node": "article_summary", "event": "completed", "article_id": 19,
                 "strategy": "stuff", "segments": 1, **coverage},
                {"node": "native", "event": "article_evidence",
                 "provenance": evidence.provenance("")}]}


def test_full_summary_requires_source_read_completion_and_published_evidence():
    observed = summary_result()
    assert not check_gold({"require_body_reads": [{"article_id": 19}]}, observed)
    assert check_gold({"require_body_reads": [{"article_id": 20}]}, observed)
    assert check_gold({"max_article_body_chars": 11}, observed)
    assert not check_gold({"max_article_body_chars": 12}, observed)


def test_multi_page_summary_requires_contiguous_source_ranges():
    observed = summary_result()
    events = observed["native_events"]
    events[0]["end"] = 6
    events.insert(1, {**events[0], "offset": 6, "end": 12})
    events[2]["pages"] = 2
    events[-1]["provenance"]["document_summaries"][0]["coverage"]["pages"] = 2
    assert not check_gold({"require_body_reads": [{"article_id": 19}]}, observed)
    events[1]["offset"] = 7
    assert check_gold({"require_body_reads": [{"article_id": 19}]}, observed)


@pytest.mark.parametrize("failure", ["missing_source", "missing_completion", "unpublished",
    "incomplete", "wrong_revision", "bool_total", "wrong_strategy", "gap", "different_article"])
def test_incomplete_or_unverified_summaries_cannot_satisfy_body_read_evidence(failure):
    observed = summary_result()
    events = observed["native_events"]
    published = events[-1]["provenance"]["document_summaries"][0]
    if failure == "missing_source":
        events.pop(0)
    elif failure == "missing_completion":
        events.pop(1)
    elif failure == "unpublished":
        events[-1]["provenance"]["document_summaries"] = []
    elif failure == "incomplete":
        published["coverage"]["complete"] = False
    elif failure == "wrong_revision":
        events[1]["revision"] = "b" * 64
    elif failure == "bool_total":
        published["coverage"]["total_chars"] = True
    elif failure == "wrong_strategy":
        published["strategy"] = []
    elif failure == "gap":
        events[0]["offset"] = 1
    else:
        published["id"] = 20
    assert check_gold({"require_body_reads": [{"article_id": 19}]}, observed)


@pytest.mark.parametrize("change", [{"revision": "unknown"}, {"start": True}, {"end": 0},
                                    {"visible_chars": 0}, {"visible_chars": 1},
                                    {"section_id": None}])
def test_malformed_or_unlocated_ranges_do_not_establish_success(change):
    observed = deepcopy(result("body"))
    observed["native_events"][0]["provenance"]["body_reads"][0]["ranges"][0].update(change)
    assert check_gold({"require_body_reads": [{}]}, observed)


@pytest.mark.parametrize("wanted", [None, {}, [None], [{"article_id": True}],
                                    [{"article_id": 0}], [{"section": "s1"}]])
def test_invalid_requirements_fail_before_any_request(tmp_path, wanted):
    path = tmp_path / "suite.jsonl"
    path.write_text(json.dumps({"id": "body", "identity": "user", "tags": ["read"],
                               "user_input": "Read source", "gold": {
                                   "require_body_reads": wanted}}) + "\n")
    with pytest.raises(ValueError, match="body-read requirement"):
        load_suite(path)
