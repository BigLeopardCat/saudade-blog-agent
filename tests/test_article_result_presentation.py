"""Model-visible source locators and execution failure status must agree."""

from types import SimpleNamespace

import pytest
from _memory_support import Capture, config
from langchain_core.messages import ToolMessage

from agent.products.blog.articles import ArticleMiddleware
from agent.runtime import tool_execution
from agent.runtime.session import ToolSession
from rag.reading import candidate_text, render_search_result, search_text
from rag.vector_index import rrf_fuse
from tools.notes import rag_search


def hit(ident=1, sections=("s1",), ranges=None):
    return {
        "type": "note",
        "id": ident,
        "title": "Synthetic source",
        "document_revision": "a" * 64,
        "section_schema": "test-schema",
        "sections": ["重复标题"] * len(sections),
        "section_ids": list(sections),
        "matches": ranges
        or [{"section_id": section, "offset": 0, "end": 40} for section in sections],
    }


def test_section_rrf_does_not_let_three_lexical_sections_hide_the_dense_target():
    row = rrf_fuse([hit(sections=("s1", "s2", "s3"))], [hit(sections=("s22",))], 60, 1)[0]
    assert row["section_ids"] == ["s1", "s22", "s2"]
    assert len(row["matches"]) == 3
    wire = candidate_text(1, row)
    assert "section=s22" in wire and "section=s3 " not in wire


def test_range_rrf_keeps_a_deep_dense_match_within_one_long_section():
    lexical = hit(ranges=[{"section_id": "s1", "offset": n, "end": n + 40} for n in (0, 500, 1000)])
    dense = hit(ranges=[{"section_id": "s1", "offset": 30000, "end": 30040}])
    row = rrf_fuse([lexical], [dense], 60, 1)[0]
    assert [match["offset"] for match in row["matches"]] == [0, 30000, 500]
    assert "offset=30000 max_chars=40" in candidate_text(1, row)


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
    assert "offset=0 max_chars=40" in candidate_text(1, row)


def test_candidate_budget_keeps_whole_visible_entries_and_their_evidence():
    rows = [hit(1), hit(2), hit(3)]
    meta = {"retrieval": {"route": "hybrid"}}
    text = search_text(meta, "\n".join(candidate_text(i + 1, row) for i, row in enumerate(rows)))
    cap = len(text) - 100
    presented = render_search_result(rows, text, meta, cap)
    assert presented.kind == "ok" and 0 < len(presented.data) < len(rows)
    assert len(presented.text) <= cap
    assert presented.meta["presentation"] == {
        "shown_candidates": len(presented.data),
        "omitted_candidates": len(rows) - len(presented.data),
    }
    for index, row in enumerate(presented.data):
        assert candidate_text(index + 1, row) in presented.text
    for row in rows[len(presented.data) :]:
        assert f"id={row['id']} " not in presented.text


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
