"""Only current, visible, exact source ranges may establish body-read evidence."""

from copy import deepcopy

import pytest

from agent.domains.articles.evidence import ArticleEvidence
from rag.corpus import content_revision
from rag.passages import attach_passages
from rag.reading import render_search_result, search_frame
from rag.search import RagIndex
from rag.source import BodyPassage
from tools.result import ok


@pytest.fixture
def source():
    body = "## Intro\n背景。\n## Workers\n" + "取消后等待工作线程退出，再归还许可。" * 30
    document = {"type": "note", "id": 41, "title": "Task lifecycle", "content": body}
    document["revision"] = content_revision(document["title"], body)
    index = RagIndex(corpus_loader=lambda: [document], vector_warmer=lambda _: None)
    index.build()
    hits = index._lexical_ranked("归还许可", 1, article_id=41)
    return document, index, hits


def test_verified_snapshot_supplies_exact_body_without_another_fetch(source):
    document, index, hits = source
    before = deepcopy(hits)
    rows = index.read_passages(hits)
    assert hits == before
    passage = BodyPassage.model_validate(rows[0]["passages"][0])
    assert "归还许可" in passage.text and passage.text in document["content"]
    assert passage.source.revision == hits[0]["document_revision"]
    evidence = ArticleEvidence()
    evidence.observe(
        "rag_search",
        {},
        ok(search_frame(rows, {}), data=rows, meta={"body_evidence": {"verified": True}}),
    )
    assert evidence.documents == {41: document["title"]}
    assert evidence.read_ranges[41][0]["visible_chars"] == len(passage.text)
    assert evidence.read_ranges[41][0]["selected_total_chars"] is None
    assert evidence.continuations == {}  # A retrieved passage never claims a full-body cursor.


@pytest.mark.parametrize(
    "field", ["corpus_revision", "content_revision", "document_revision", "section_schema"]
)
def test_snapshot_or_source_version_mismatch_fails_closed(source, field):
    _, index, hits = source
    hits[0][field] = "changed"
    with pytest.raises(ValueError, match="snapshot changed"):
        index.read_passages(hits)


def test_corpus_refresh_between_search_and_resolution_cannot_mix_source_versions(source):
    document, index, hits = source
    document["content"] += "新的正文。"
    index.build()
    with pytest.raises(ValueError, match="snapshot changed"):
        index.read_passages(hits)


@pytest.mark.parametrize("start,end", [(True, 3), (-1, 4), (4, 3), (0, 999999)])
def test_invalid_locator_never_returns_body_text(source, start, end):
    _, index, hits = source
    hits[0]["matches"][0].update(offset=start, end=end)
    with pytest.raises(ValueError, match="locator differs"):
        index.read_passages(hits)


def test_unverified_snapshot_passages_do_not_establish_body_evidence(source):
    _, index, hits = source
    rows = index.read_passages(hits)
    evidence = ArticleEvidence()
    evidence.observe("rag_search", {}, ok(search_frame(rows, {}), data=rows))
    assert evidence.candidates and evidence.documents == evidence.read_ranges == {}


def test_budget_removed_passages_never_enter_read_evidence(source):
    _, index, hits = source
    rows = index.read_passages(hits)
    meta = {"body_evidence": {"verified": True, "scope": "returned_passages"}}
    no_body = [{**rows[0], "passages": []}]
    # Leave room for metadata and a locator, but not the retrieved passage.
    cap = len(
        search_frame(
            no_body, {**meta, "body_evidence": {**meta["body_evidence"], "returned_chars": 0}}
        )
    )
    result = render_search_result(rows, meta, cap)
    assert result.kind == "ok" and result.data[0]["passages"] == []
    assert result.meta["body_evidence"]["returned_chars"] == 0
    evidence = ArticleEvidence()
    evidence.observe("rag_search", {}, result)
    assert evidence.candidates and evidence.documents == evidence.read_ranges == {}
    assert rows[0]["passages"]


def test_global_passage_budget_keeps_exact_visible_range(source):
    document, index, hits = source
    rows = attach_passages(
        hits,
        {(document["type"], document["id"]): document},
        index.status()["revision"],
        max_chars=5,
    )
    passage = BodyPassage.model_validate(rows[0]["passages"][0])
    assert len(passage.text) == 5 and passage.source.end - passage.source.offset == 5


def test_body_passage_rejects_mismatched_text_length_and_unknown_parser(source):
    _, index, hits = source
    passage = index.read_passages(hits)[0]["passages"][0]
    with pytest.raises(ValueError):
        BodyPassage.model_validate({**passage, "text": passage["text"] + "extra"})
    with pytest.raises(ValueError):
        BodyPassage.model_validate(
            {**passage, "source": {**passage["source"], "section_schema": "unsupported"}}
        )


def test_model_projection_shares_one_version_without_losing_readable_coordinates(source):
    import json

    _, index, hits = source
    rows = index.read_passages(hits)
    wire = search_frame(rows, {"body_evidence": {"verified": True}})
    doc = json.loads(wire)["documents"][0]
    assert wire.count(hits[0]["document_revision"]) == 1
    assert doc["source"]["revision"] == hits[0]["document_revision"]
    for passage, raw in zip(doc["passages"], rows[0]["passages"]):
        assert passage["text"] == raw["text"]
        assert {**doc["source"], **passage["range"]} == raw["source"]
    for request in doc["read_requests"]:
        assert "revision" not in request["request"]
        args = request["request"]
        assert args["article_id"] == doc["source"]["id"]
        assert args["doc_type"] == doc["source"]["type"]
