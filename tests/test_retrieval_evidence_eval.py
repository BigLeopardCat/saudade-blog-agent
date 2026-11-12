"""Exact source provenance and location coverage must not be replaced by article hits."""

import copy

import pytest

from eval.offline_rag_eval import evaluate
from eval.retrieval_evidence import score, validate
from rag.corpus import content_revision
from rag.sections import PARSER_VERSION


@pytest.fixture
def annotated():
    corpus = {"a": {"title": "A", "text": "# Intro\nbackground\n# Evidence\nrequired fact\n"}}
    span = {"document_id": "a", "section_id": "s2", "start": 0, "end": 13,
            "quote": "required fact", "section_schema": PARSER_VERSION,
            "revision": content_revision("A", corpus["a"]["text"])}
    query = {"text": "what is required?", "evidence_spans": [[span]]}
    return corpus, query


def test_article_hit_without_target_section_is_not_evidence_coverage(annotated):
    corpus, query = annotated
    validate(query, corpus, {"a"})
    data = {"queries": {"q": query}, "qrels": {"q": {"a": 1}}}
    loc = {"q": [{"document_id": "a", "matches": [
        {"section_id": "s1", "offset": 0, "end": 50}]}]}
    row = evaluate(data, {"q": {"a": 1}}, [1], locations=loc)["per_query"]["q"]
    assert row["Success@1"] == 1
    assert row["PassageEvidenceCoverage@1"] == row["AllPassageEvidence@1"] == 0
    assert "PassageEvidenceCoverage@1" not in evaluate(data, {"q": {"a": 1}}, [1])[
        "per_query"]["q"]


def test_full_span_may_be_covered_by_adjacent_chunks_but_not_a_gap(annotated):
    _, query = annotated
    matches = [{"section_id": "s2", "offset": 0, "end": 5},
               {"section_id": "s2", "offset": 5, "end": 13}]
    hits = [{"document_id": "a", "matches": matches}]
    assert score(query["evidence_spans"], hits, 1)["AllPassageEvidence@1"] == 1
    matches[1]["offset"] = 6
    assert score(query["evidence_spans"], hits, 1)["AllPassageEvidence@1"] == 0


@pytest.mark.parametrize("field,value", [
    ("revision", "stale"), ("section_id", "s3"), ("section_schema", "unknown"),
    ("start", 1), ("end", 999), ("quote", "fabricated fact"), ("document_id", "b"),
])
def test_changed_or_invented_source_annotations_are_rejected(annotated, field, value):
    corpus, query = annotated
    query["evidence_spans"][0][0][field] = value
    with pytest.raises(ValueError):
        validate(query, corpus, {"a"})


def test_alternatives_and_multiple_required_groups_have_different_denominators(annotated):
    _, query = annotated
    span = query["evidence_spans"][0][0]
    other = {**span, "section_id": "s3"}
    hits = [{"document_id": "a", "matches": [{"section_id": "s2", "offset": 0, "end": 13}]}]
    assert score([[span, other]], hits, 1)["PassageEvidenceCoverage@1"] == 1
    assert score([[span], [other]], hits, 1) == {
        "PassageEvidenceCoverage@1": 0.5, "AllPassageEvidence@1": 0.0}


def test_scope_cannot_exclude_a_positive_and_locations_cannot_change_rank(annotated):
    corpus, query = annotated
    query["scope_document_id"] = "b"
    with pytest.raises(ValueError, match="scope"):
        validate(query, corpus, {"a"})
    data = {"queries": {"q": copy.deepcopy(query)}, "qrels": {"q": {"a": 1}}}
    with pytest.raises(ValueError, match="ranks"):
        evaluate(data, {"q": {"a": 1}}, [1], locations={"q": []})
