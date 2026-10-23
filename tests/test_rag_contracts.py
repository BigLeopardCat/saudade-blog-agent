"""Behavioral regressions for the versioned RAG pipeline (no network/LLM)."""

import copy
import json
import threading
from array import array

import pytest

from config import settings
from rag import search as retrieval
from rag import vector_index as vectors
from rag.chunks import MAX_CHARS
from rag.corpus import CorpusUnavailable, content_revision, corpus_revision, fetch
from rag.reading import RevisionChanged, article_page
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
    ix = index(monkeypatch, [{"type": "note", **doc(1, "alpha " * 100, "t")}])
    assert ix._avgdl == 101
    assert ix._lengths == [101]


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


@pytest.mark.parametrize("stage", ["f32", "json", "manifest"])
def test_interrupted_vector_commit_preserves_exact_old_document_vectors(
    monkeypatch, tmp_path, stage
):
    monkeypatch.setattr(settings, "embedding_model", "fixture-model")
    monkeypatch.setattr(settings, "embedding_dim", 2)
    monkeypatch.setattr(
        vectors,
        "_embed_texts",
        lambda texts, space: [[1.0, 0.0] if "first" in t else [0.0, 1.0] for t in texts],
    )
    chunks = [
        {"type": "note", "id": i, "title": str(i), "section": "s", "text": t}
        for i, t in [(1, "first"), (2, "second")]
    ]
    store = vectors.VectorStore(tmp_path)
    space = vectors.space_from_settings()
    store.update(chunks, space)
    fingerprint = vectors.corpus_fingerprint(space, chunks)
    before = [list(row) for row in store.view_for(fingerprint).vectors]
    real = vectors._write_atomic

    def interrupt(path, data):
        real(path, data)
        if stage == "manifest" and path.name != "manifest.json" and path.suffix == ".json":
            raise OSError("interrupted before manifest")
        if stage != "manifest" and path.suffix == "." + stage:
            raise OSError("interrupted after generation file")

    monkeypatch.setattr(vectors, "_write_atomic", interrupt)
    changed = [{**c, "text": c["text"] + " changed"} for c in chunks]
    with pytest.raises(OSError):
        store.update(changed, space)
    reader = vectors.VectorStore(tmp_path)
    assert reader.load()
    assert [list(row) for row in reader.view_for(fingerprint).vectors] == before
    assert reader.view_for(vectors.corpus_fingerprint(space, changed)) is None


def test_same_length_vector_corruption_is_detected(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "embedding_dim", 2)
    monkeypatch.setattr(vectors, "_embed_texts", lambda texts, space: [[1, 0] for _ in texts])
    chunks = [{"type": "note", "id": 1, "title": "T", "section": "s", "text": "body"}]
    store = vectors.VectorStore(tmp_path)
    store.update(chunks, vectors.space_from_settings())
    manifest = json.loads(store.manifest_path.read_text())
    path = tmp_path / manifest["cache_files"]["f32"]
    path.write_bytes(array("f", [0, 1]).tobytes())
    assert vectors.VectorStore(tmp_path).load() is False


def test_query_cache_is_partitioned_by_space_and_checks_cached_dimensions(monkeypatch, tmp_path):
    monkeypatch.setattr(vectors, "_query_cache", {})
    monkeypatch.setattr(vectors, "_query_order", [])
    monkeypatch.setattr(settings, "embedding_model", "a")
    monkeypatch.setattr(settings, "embedding_dim", 2)
    store = vectors.VectorStore(tmp_path)
    monkeypatch.setattr(vectors, "_store", store)
    store._view_space = vectors.space_from_settings()
    store._view = vectors.VectorView([array("f", [1, 0])], 2)
    calls = []

    def embed(texts, space):
        calls.append(space.model)
        return [[1.0] + [0.0] * (space.dim - 1)]

    monkeypatch.setattr(vectors, "_embed_texts", embed)
    assert len(vectors.embed_query("query")) == 2
    monkeypatch.setattr(settings, "embedding_model", "b")
    monkeypatch.setattr(settings, "embedding_dim", 3)
    store._view_space = vectors.space_from_settings()
    store._view = vectors.VectorView([array("f", [1, 0, 0])], 3)
    assert len(vectors.embed_query("query")) == 3
    assert calls == ["a", "b"]
    with pytest.raises(ValueError):
        vectors.cos_sim([1, 0], [1, 0, 0])


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
    retrieval.invalidate_document(1, original[0]["revision"])
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
