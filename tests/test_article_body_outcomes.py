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
