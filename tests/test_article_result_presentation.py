"""Model-visible source locators and execution failure status must agree."""

import json
from types import SimpleNamespace

import pytest
from _memory_support import Capture, config
from langchain_core.messages import ToolMessage

from agent.products.blog.articles import ArticleMiddleware
from agent.runtime import tool_execution
from agent.runtime.session import ToolSession
from rag.reading import candidate_text, render_search_result, search_frame
from rag.sections import PARSER_VERSION
from rag.vector_index import rrf_fuse
from tools.notes import rag_search


def hit(ident=1, sections=("s1",), ranges=None):
    return {
        "type": "note",
        "id": ident,
        "title": "Synthetic source",
        "document_revision": "a" * 64,
        "section_schema": PARSER_VERSION,
        "sections": ["重复标题"] * len(sections),
        "section_ids": list(sections),
        "matches": ranges
        or [{"section_id": section, "offset": 0, "end": 40} for section in sections],
    }


def test_section_rrf_does_not_let_three_lexical_sections_hide_the_dense_target():
    row = rrf_fuse([hit(sections=("s1", "s2", "s3"))], [hit(sections=("s22",))], 60, 1)[0]
    assert row["section_ids"] == ["s1", "s22", "s2", "s3"]
    assert len(row["matches"]) == 4
    wire = candidate_text(1, row)
    assert {r["request"]["selection"]["section"] for r in json.loads(wire)["read_requests"]} == set(
        row["section_ids"]
    )


def test_ranked_union_retains_the_second_dense_section_and_its_readable_range():
    lexical = hit(sections=("s3", "s2", "s6"))
    dense = hit(sections=("s19", "s22", "s3"))
    row = rrf_fuse([lexical], [dense], 60, 1)[0]
    assert row["section_ids"][:3] == ["s3", "s19", "s2"]
    assert set(row["section_ids"]) == {"s2", "s3", "s6", "s19", "s22"}
    assert {match["section_id"] for match in row["matches"]} == set(row["section_ids"])
    wire = candidate_text(1, row)
    reads = [item["request"]["selection"] for item in json.loads(wire)["read_requests"]]
    assert any(r["section"] == "s22" and r["offset"] == 0 and r["max_chars"] == 40 for r in reads)
    assert json.loads(wire)["source"]["revision"] == row["document_revision"]


def test_disjoint_retrievers_keep_all_six_bounded_sections_visible():
    row = rrf_fuse([hit(sections=("s1", "s2", "s3"))], [hit(sections=("s4", "s5", "s6"))], 60, 1)[0]
    assert len(row["section_ids"]) == len(row["matches"]) == 6
    wire = candidate_text(1, row)
    assert {r["request"]["selection"]["section"] for r in json.loads(wire)["read_requests"]} == {
        f"s{n}" for n in range(1, 7)
    }


def test_range_rrf_keeps_a_deep_dense_match_within_one_long_section():
    lexical = hit(ranges=[{"section_id": "s1", "offset": n, "end": n + 40} for n in (0, 500, 1000)])
    dense = hit(ranges=[{"section_id": "s1", "offset": 30000, "end": 30040}])
    row = rrf_fuse([lexical], [dense], 60, 1)[0]
    assert [match["offset"] for match in row["matches"]] == [0, 30000, 500]
    assert any(
        r["request"]["selection"]["offset"] == 30000
        and r["request"]["selection"]["max_chars"] == 40
        for r in json.loads(candidate_text(1, row))["read_requests"]
    )


@pytest.mark.parametrize(
    "field", ["document_revision", "content_revision", "corpus_revision", "section_schema"]
)
def test_rrf_rejects_locations_from_different_source_versions(field):
    with pytest.raises(ValueError, match="different source"):
        rrf_fuse([hit()], [{**hit(), field: "other"}], 60, 1)


def test_rrf_never_reuses_invalid_character_coordinates():
    invalid = [
        {"section_id": "s1", "offset": start, "end": end}
        for start, end in [(True, 40), (-1, 40), (10, 10), (0, "40")]
    ]
    row = rrf_fuse([hit(ranges=invalid)], [hit()], 60, 1)[0]
    assert row["matches"] == [{"section_id": "s1", "offset": 0, "end": 40}]
    request = json.loads(candidate_text(1, row))["read_requests"][0]["request"]
    assert request["selection"]["offset"] == 0


def test_candidate_budget_keeps_whole_visible_entries_and_their_evidence():
    rows = [hit(1), hit(2), hit(3)]
    meta = {"retrieval": {"route": "hybrid"}}
    text = search_frame(rows, meta)
    cap = len(text) - 100
    presented = render_search_result(rows, meta, cap)
    assert presented.kind == "ok" and 0 < len(presented.data) < len(rows)
    assert len(presented.text) <= cap
    assert presented.meta["presentation"] == {
        "shown_candidates": len(presented.data),
        "omitted_candidates": len(rows) - len(presented.data),
    }
    for index, row in enumerate(presented.data):
        assert candidate_text(index + 1, row) in presented.text
    assert [r["id"] for r in json.loads(presented.text)["documents"]] == [
        r["id"] for r in presented.data
    ]


def execute(monkeypatch, source, limit, *, upgrade=False):
    session = ToolSession(Capture(), config(), {"rag_search": rag_search}, {})
    middleware = ArticleMiddleware(session)
    if upgrade:
        session.render_result = lambda call, value, cap: value.model_copy(
            update={"artifact": {"kind": "ok", "data": [hit()]}, "status": "success"}
        )
    records, prompt = [], []
    monkeypatch.setattr(tool_execution, "result_limit", lambda _: limit)
    monkeypatch.setattr(
        tool_execution, "record", lambda *args, **kwargs: records.append((args, kwargs))
    )
    call = {
        "id": "search",
        "name": "rag_search",
        "args": {"query": "synthetic"},
        "type": "tool_call",
    }
    request = SimpleNamespace(tool_call=call, override=lambda **kwargs: None)
    tool_execution.append_result(session, request, lambda _: source, prompt, 1, [])
    return prompt[-1], middleware.evidence, records


def test_no_locator_fits_is_an_unavailable_model_result_and_never_empty_evidence(monkeypatch):
    source = ToolMessage(
        content=candidate_text(1, hit()),
        tool_call_id="search",
        artifact={"kind": "ok", "data": [hit()], "meta": {"retrieval": {"route": "hybrid"}}},
    )
    message, evidence, records = execute(monkeypatch, source, 10)
    assert message.status == "error" and message.artifact["kind"] == "unavailable"
    assert message.artifact["data"] is None
    assert message.artifact["meta"]["reason"] == "presentation_budget"
    assert evidence.failed and not evidence.empty and evidence.candidates == {}
    assert any(
        args == ("native", "call") and kwargs["kind"] == "unavailable" for args, kwargs in records
    )


def test_unseen_candidates_do_not_become_source_evidence(monkeypatch):
    rows = [hit(1), hit(2), hit(3)]
    source = ToolMessage(
        content="\n".join(candidate_text(i + 1, row) for i, row in enumerate(rows)),
        tool_call_id="search",
        artifact={"kind": "ok", "data": rows, "meta": {"retrieval": {"route": "hybrid"}}},
    )
    message, evidence, _ = execute(monkeypatch, source, len(source.content) - 100)
    assert message.status == "success"
    assert 0 < len(evidence.candidates) < len(rows)
    assert set(evidence.candidates) == {row["id"] for row in message.artifact["data"]}
    assert evidence.documents == {} and evidence.read_ranges == {}


@pytest.mark.parametrize(
    "kind,status,expected", [("unavailable", "success", "unavailable"), ("ok", "error", "error")]
)
def test_presentation_cannot_upgrade_an_existing_tool_failure(monkeypatch, kind, status, expected):
    message, _, records = execute(
        monkeypatch,
        ToolMessage(
            content="failed", tool_call_id="search", status=status, artifact={"kind": kind}
        ),
        100,
        upgrade=True,
    )
    assert message.status == "error" and message.artifact["kind"] == expected
    assert any(
        args == ("native", "call") and kwargs["kind"] == expected for args, kwargs in records
    )


@pytest.mark.parametrize("budget,complete", [(12000, True), (1200, False)])
def test_wire_json_budget_preserves_true_body_boundaries(monkeypatch, budget, complete):
    from agent.runtime.message_content import estimate_tokens
    from config.settings import settings
    from eval.fixtures.document_reads import article_page

    monkeypatch.setattr(settings, "agent_tool_result_tokens", budget)
    body = '正文含双引号 "quoted" 和换行\n' * 400
    data = article_page({"noteTitle": "Source", "noteContent": body}, 1)
    source = ToolMessage(content=str(data), name="get_article_detail", tool_call_id="read",
                         artifact={"kind": "ok", "data": data})
    middleware = ArticleMiddleware(ToolSession(Capture(), config(), {}, {}))
    message = middleware.render_result(
        {"name": "get_article_detail", "args": {"article_id": 1}}, source, 1)
    visible = message.artifact["data"]
    assert json.loads(message.content) == visible
    assert estimate_tokens([message]) <= budget
    assert visible["readComplete"] is complete
    assert visible["noteContent"] == body[:visible["source"]["end"]]
    assert visible["nextOffset"] == (None if complete else visible["source"]["end"])
    if complete:
        assert visible["noteContent"] == body and visible["source"]["end"] == len(body)
    else:
        assert 0 < visible["source"]["end"] < len(body)
    assert data["noteContent"] == body and data["readComplete"]
