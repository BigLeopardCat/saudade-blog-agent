"""Behavioral regressions for the versioned RAG pipeline (no network/LLM)."""

import copy
import json
import threading

import pytest
from _rag_store import memory_store, memory_vectors  # noqa: F401

from config import settings
from eval.fixtures.document_reads import RevisionChanged, article_page
from rag import search as retrieval
from rag import vector_index as vectors
from rag.chunks import MAX_CHARS
from rag.corpus import CorpusUnavailable, content_revision, corpus_revision, fetch
from rag.sections import reading_revision
from tools.result import unavailable


def doc(article_id, content="body", title="Article"):
    return {
        "id": article_id,
        "title": title,
        "content": content,
        "revision": content_revision(title, content),
    }


def page(documents, total=None, cursor=None, revision="a" * 64):
    return {
        "schema_version": 1,
        "revision": revision,
        "total": len(documents) if total is None else total,
        "documents": documents,
        "next_cursor": cursor,
    }


def index(monkeypatch, documents):
    monkeypatch.setattr(retrieval, "warm_vectors", lambda chunks: None)
    ix = retrieval.RagIndex()
    monkeypatch.setattr(ix, "_fetch_corpus", lambda: documents)
    ix.build()
    return ix


def test_incomplete_snapshot_retains_last_success_and_reports_staleness(monkeypatch):
    import tools.base as base

    old = [{"type": "note", **doc(51, "needle")}]
    ix = index(monkeypatch, old)
    last_success = ix._last_build
    first = [doc(i, "other") for i in range(1, 51)]
    responses = iter([page(first, total=51, cursor=50), unavailable("outage")])
    monkeypatch.setattr(ix, "_fetch_corpus", lambda: fetch(base._get))
    monkeypatch.setattr(base, "_get", lambda path: next(responses))
    with pytest.raises(CorpusUnavailable):
        ix.build()
    assert [h["id"] for h in ix.search("needle")] == [51]
    assert ix._last_build == last_success
    assert ix.status()["stale"] and ix.status()["error"] == "CorpusUnavailable"


@pytest.mark.parametrize(
    "bad",
    [
        unavailable("outage"),
        page([], total=1),
        page([doc(1)], total=2),
        page([doc(1), doc(1)]),
        page([doc(1)], total=2, cursor=2),
        page([{**doc(1), "content": "tampered"}]),
    ],
)
def test_invalid_pages_never_commit_a_partial_corpus(bad):
    with pytest.raises(CorpusUnavailable):
        fetch(lambda path: bad)


def test_revision_change_between_pages_is_rejected():
    first = [doc(i) for i in range(1, 51)]
    responses = iter(
        [page(first, total=51, cursor=50), page([doc(51)], total=51, revision="b" * 64)]
    )
    with pytest.raises(CorpusUnavailable, match="revision_changed"):
        fetch(lambda path: next(responses))


def test_empty_snapshot_is_distinct_from_a_source_failure(monkeypatch):
    assert fetch(lambda path: page([])) == []
    ix = index(monkeypatch, [])
    assert ix.search("anything") == []
    assert ix.status()["stale"] is False


def test_snapshot_cursor_carries_revision_and_no_n_plus_one_requests():
    first = [doc(i) for i in range(1, 51)]
    responses = iter([page(first, total=51, cursor=50), page([doc(51)], total=51)])
    paths = []

    def get(path):
        paths.append(path)
        return next(responses)

    assert len(fetch(get)) == 51
    assert len(paths) == 2 and "after=50" in paths[1] and "revision=" in paths[1]


def test_heading_paths_and_bounded_chunks_are_searchable(monkeypatch):
    text = "## Kubernetes\n### 部署\n" + "普通说明。" * 10000
    ix = index(monkeypatch, [{"type": "note", **doc(1, text, "操作手册")}])
    assert ix.search("Kubernetes")[0]["id"] == 1
    assert all(len(c["text"]) <= MAX_CHARS for c in ix._chunks)
    assert all(c["section_path"] == ["Kubernetes", "部署"] for c in ix._chunks)
    assert all(c["section_id"] == "s2" for c in ix._chunks)
    assert ix.search("Kubernetes")[0]["section_ids"] == ["s2"]


def test_bm25_normalizes_using_total_token_length(monkeypatch):
    import bm25s

    ix = index(monkeypatch, [{"type": "note", **doc(1, "alpha " * 100, "t")},
                             {"type": "note", **doc(2, "alpha " + "beta " * 99, "t")}])
    reference = bm25s.BM25(k1=retrieval.BM25_K1, b=retrieval.BM25_B,
                           method="lucene", dtype="float64")
    reference.index([["t", *(["alpha"] * 100)], ["t", "alpha", *(["beta"] * 99)]],
                    show_progress=False)
    expected = reference.get_scores(["alpha"])
    actual = {row["id"]: row["score"] for row in ix._lexical_ranked("alpha", 10)}
    assert actual == pytest.approx({1: expected[0], 2: expected[1]}, abs=5e-5)


@pytest.mark.parametrize("rows,expected", [
    ([doc(1, "!!!", "!!!")], []),
    ([doc(1, "!!!", "!!!"), doc(2, "alpha", "Text")], [2]),
])
def test_lexical_index_handles_empty_token_documents(monkeypatch, rows, expected):
    ix = index(monkeypatch, [{"type": "note", **row} for row in rows])
    assert [row["id"] for row in ix._lexical_ranked("alpha", 10)] == expected
    assert ix._lexical_ranked("unseenword", 10) == []
    assert ix.status()["lexical_backend"]["name"] == "bm25s"


def test_concurrent_build_has_only_one_source_fetch(monkeypatch):
    ix = retrieval.RagIndex()
    entered, release = threading.Event(), threading.Event()
    calls = []

    def get():
        calls.append(1)
        entered.set()
        assert release.wait(2)
        return [{"type": "note", **doc(1)}]

    monkeypatch.setattr(ix, "_fetch_corpus", get)
    monkeypatch.setattr(retrieval, "warm_vectors", lambda chunks: None)
    worker = threading.Thread(target=ix.build)
    worker.start()
    assert entered.wait(2)
    assert ix.build() is False
    release.set()
    worker.join(2)
    assert calls == [1] and not worker.is_alive()


def test_range_reads_recover_the_entire_long_section_without_gaps():
    content = "## 超长节\n" + ("abcdefghij" * 4000) + "TAIL"
    data = {"noteTitle": "Long", "noteContent": content, "content": content}
    pieces, offset, revision = [], 0, ""
    while True:
        result = article_page(data, 1, offset=offset, max_chars=1024, revision=revision)
        assert len(result["noteContent"]) <= 1024 and "content" not in result
        pieces.append(result["noteContent"])
        revision = result["contentRevision"]
        if result["nextOffset"] is None:
            break
        offset = result["nextOffset"]
    assert "".join(pieces) == content
    with pytest.raises(RevisionChanged):
        article_page({**data, "noteContent": "changed"}, 1, offset=1, revision=revision)


def test_duplicate_sections_require_unambiguous_revision_bound_ids():
    data = {"noteTitle": "T", "noteContent": "## 配置\nfirst\n## 配置\nsecond"}
    ambiguous = article_page(data, 1, "配置")
    assert ambiguous["sectionText"] == "" and len(ambiguous["sections"]) == 2
    with pytest.raises(ValueError, match="contentRevision"):
        article_page(data, 1, "s2")
    selected = article_page(data, 1, "s2", revision=ambiguous["contentRevision"])
    assert "second" in selected["sectionText"] and "first" not in selected["sectionText"]


def test_query_cache_is_partitioned_by_space_and_checks_cached_dimensions(monkeypatch, tmp_path):
    monkeypatch.setattr(vectors, "_query_cache", {})
    monkeypatch.setattr(vectors, "_query_order", [])
    monkeypatch.setattr(settings, "embedding_model", "a")
    monkeypatch.setattr(settings, "embedding_dim", 2)
    store = memory_store(tmp_path)
    monkeypatch.setattr(vectors, "_store", store)
    calls = []

    def embed(texts, space):
        calls.append(space.model)
        return [[1.0] + [0.0] * (space.dim - 1)]

    monkeypatch.setattr(vectors, "_embed_texts", embed)
    assert len(vectors.embed_query("query")) == 2
    monkeypatch.setattr(settings, "embedding_model", "b")
    monkeypatch.setattr(settings, "embedding_dim", 3)
    assert len(vectors.embed_query("query")) == 3
    assert calls == ["a", "b"]


def test_corpus_identity_changes_on_title_body_and_membership():
    original = [{"type": "note", **doc(1)}]
    modified = copy.deepcopy(original)
    modified[0]["content"] = "changed"
    assert corpus_revision(original) != corpus_revision(modified)
    modified = copy.deepcopy(original)
    modified[0]["title"] = "changed"
    assert corpus_revision(original) != corpus_revision(modified)
    assert corpus_revision(original) != corpus_revision([])


def test_authoritative_mismatch_is_not_lost_during_an_inflight_build(monkeypatch):
    original = [{"type": "note", **doc(1, "old")}]
    ix = index(monkeypatch, original)
    monkeypatch.setattr(retrieval, "_index", ix)
    entered, release = threading.Event(), threading.Event()

    def slow_snapshot():
        entered.set()
        assert release.wait(2)
        return original

    monkeypatch.setattr(ix, "_fetch_corpus", slow_snapshot)
    worker = threading.Thread(target=ix.build)
    worker.start()
    assert entered.wait(2)
    retrieval.invalidate_document(1, reading_revision(original[0]["title"], original[0]["content"]))
    release.set()
    worker.join(2)
    assert ix.status()["stale"] and not worker.is_alive()
    monkeypatch.setattr(ix, "_fetch_corpus", lambda: [{"type": "note", **doc(1, "new")}])
    ix.build()
    assert not ix.status()["stale"]


def test_rrf_preserves_revision_and_readable_ranges_from_both_arms():
    lexical = {
        "type": "note",
        "id": 1,
        "title": "Long",
        "score": 4,
        "corpus_revision": "a" * 64,
        "document_revision": "b" * 64,
        "sections": ["Late section"],
        "section_ids": ["s7"],
        "matches": [{"section_id": "s7", "offset": 29000, "end": 30000}],
    }
    vector = {
        **lexical,
        "score": 0.9,
        "section_ids": ["s8"],
        "matches": [{"section_id": "s8", "offset": 100, "end": 1500}],
    }
    for left, right in [([lexical], [vector]), ([], [vector])]:
        result = vectors.rrf_fuse(left, right, 60, 3)[0]
        assert result["document_revision"] == lexical["document_revision"]
        assert result["corpus_revision"] == lexical["corpus_revision"]
        assert result["matches"][0] == (lexical if left else vector)["matches"][0]
        assert result["section_ids"][0] == (lexical if left else vector)["section_ids"][0]
    assert lexical["matches"] == [{"section_id": "s7", "offset": 29000, "end": 30000}]


def test_ranked_candidates_expose_bounded_section_locators_without_guessing_offsets():
    from rag.reading import candidate_text

    hit = {
        "title": "文档",
        "type": "note",
        "id": 46,
        "score": 0.9,
        "sections": ["概述", "布局", "交互", "额外"],
        "section_ids": ["s1", "s20", "s18", "s99"],
        "document_revision": "rev1",
        "matches": [
            {"section_id": "s1", "offset": 0},
            {"section_id": "s1", "offset": 50},
            {"section_id": "s20", "offset": 96},
        ],
    }
    result = json.loads(candidate_text(1, hit))
    assert result["rank"] == 1 and "score" not in result
    assert result["passages"] == [] and result["read_requests"] == []
    assert [m["section_id"] for m in result["index_matches"]] == ["s1", "s1", "s20"]
    assert all("offset" not in m and "index_excerpt" not in m for m in result["index_matches"])


def test_candidate_preserves_source_hierarchy_for_direct_section_reading():
    from rag.chunks import split
    from rag.ranking import documents
    from rag.reading import candidate_text

    content = "# Guide\n## Layout\nOverview.\n### Current\nneedle evidence"
    chunks = [
        {**row, "type": "note", "id": 46, "title": "Guide", "reading_revision": "revision"}
        for row in split("Guide", content)
    ]
    index = next(i for i, row in enumerate(chunks) if row["section"] == "Current")
    hit = documents(chunks, {index: 1.0}, "corpus", query_terms=["needle"])[0]
    match = hit["matches"][0]
    assert match["section_path"] == ["Guide", "Layout", "Current"]
    assert (match["offset"], match["end"]) == (0, len("needle evidence"))
    wire = candidate_text(1, hit)
    candidate = json.loads(wire)
    assert candidate["index_matches"][0]["section_path"] == ["Guide", "Layout", "Current"]
    assert candidate["read_requests"] == []  # Invalid revision cannot become an executable locator.
    assert "needle evidence" in wire
    match["section_path"].append("not a source heading")
    assert chunks[index]["section_path"] == ["Guide", "Layout", "Current"]


def test_fusion_preserves_section_paths_and_rejects_conflicting_hierarchy():
    from rag.ranking import merge_match

    source = {"section_path": ["Guide", "Layout"]}
    target = {}
    merge_match(target, source)
    source["section_path"].append("Current")
    assert target["section_path"] == ["Guide", "Layout"]
    with pytest.raises(ValueError, match="conflicting section paths"):
        merge_match(target, source)
