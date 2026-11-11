"""User-visible invariants for library-backed retrieval and namespace migration."""

from collections import Counter
from dataclasses import replace

import pytest
from _rag_store import memory_store, memory_vectors  # noqa: F401

from config import settings
from rag import vector_index as vectors
from rag.chunks import MAX_CHARS, split
from rag.tokenization import tokenize


def test_scoped_passage_ranking_is_independent_of_unrelated_documents():
    from rag.lexical import LexicalIndex

    passages = [["model", "overview"] * 8, ["model", "configuration", "timeout"],
                ["configuration", "deployment"] * 6]
    local = LexicalIndex(passages, k1=1.5, b=0.75)
    unrelated = [["configuration", "unrelated"] * 12 for _ in range(50)]
    corpus = LexicalIndex([unrelated[0], *passages, *unrelated[1:]], k1=1.5, b=0.75)
    expected = local.scores(["model", "configuration"])
    scoped = corpus.scores(["model", "configuration", "unrelated"], allowed_indices=[1, 2, 3])
    assert scoped == pytest.approx({i + 1: score for i, score in expected.items()})
    assert corpus.scores(["unrelated"], allowed_indices=[1, 2, 3]) == {}
    assert corpus.scores(["model"], allowed_indices=[]) == {}
    assert corpus.scores(["model", "configuration"], allowed_indices=[1, 2, 3]) == scoped
    assert corpus._scoped.cache_info().currsize == 1


def test_mixed_language_features_keep_frequency_and_separate_language_boundaries():
    features = Counter(tokenize("博客博客 Rust_1.2 OTA中文"))
    assert features["博客"] == 2
    assert features["rust_1.2"] == features["ota"] == features["中文"] == 1
    assert tokenize("猫") == ["猫"]
    assert tokenize("中文，标题") == ["中文", "标题"]


def test_library_chunks_preserve_every_character_with_valid_repeated_source_ranges():
    body = ("重复行\n\n" * 450) + "👩🏽‍💻 é" + ("无分隔长句" * 600) + "TAIL\n "
    chunks = split("标题", body)
    covered = set()
    for chunk in chunks:
        start, end = chunk["start"], chunk["end"]
        assert chunk["text"][chunk["prefix_chars"]:] == body[start:end]
        assert len(chunk["text"]) <= MAX_CHARS
        covered.update(range(start, end))
    assert covered == set(range(len(body)))
    assert chunks[-1]["end"] == len(body)
    assert any("TAIL" in chunk["text"] for chunk in chunks)


def test_chunk_strategy_change_rejects_old_vectors_and_partitions_query_cache(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(settings, "embedding_profile", "")
    monkeypatch.setattr(settings, "embedding_model", "fixture")
    monkeypatch.setattr(settings, "embedding_dim", 2)
    space = vectors.space_from_settings()
    legacy = replace(space, chunking="markdown-path-v2")
    chunks = [{"type": "note", "id": 1, "title": "T", "section": "s", "text": "body"}]
    sent = []

    def embed(texts, _):
        sent.extend(texts)
        return [[1, 0] for _ in texts]

    store = memory_store(tmp_path)
    monkeypatch.setattr(vectors, "_store", store)
    monkeypatch.setattr(vectors, "_embed_texts", embed)
    monkeypatch.setattr(vectors, "space_from_settings", lambda: legacy)
    assert store.update(chunks, legacy)["embedded"] == 1
    assert vectors.embed_query("query") is not None
    monkeypatch.setattr(vectors, "space_from_settings", lambda: space)
    assert store.view() is None
    assert vectors.embed_query("query") is not None  # Different namespace calls the embedder.
    assert len(sent) == 3
    assert store.update(chunks, space)["embedded"] == 1
    assert store.update(chunks, space)["skipped"] == "aligned"
    assert len(sent) == 4
    assert vectors.chunk_key(space, "same") != vectors.chunk_key(legacy, "same")
    reader = memory_store(tmp_path)
    assert reader.load()
    assert reader.view_for(vectors.corpus_fingerprint(space, chunks)) is not None


def test_official_fusion_uses_document_identity_and_stable_ties_without_fake_scores():
    # Identical titles/text must not collapse different article IDs or document types.
    rows = [
        {"type": "note", "id": 1, "title": "same", "score": 999},
        {"type": "note", "id": 2, "title": "same", "score": -999},
        {"type": "other", "id": 1, "title": "same", "score": 999},
    ]
    result = vectors.rrf_fuse(rows, list(reversed(rows)), 60, 3)
    assert [(row["type"], row["id"]) for row in result] == [
        ("note", 1), ("other", 1), ("note", 2)
    ]
    assert [row["rank"] for row in result] == [1, 2, 3]
    assert all("score" not in row for row in result)


def test_fusion_rank_is_independent_of_retriever_score_scales():
    lexical = [{"type": "note", "id": i, "score": 9 - i} for i in [1, 2, 3]]
    vector = [{"type": "note", "id": i, "score": 0.9} for i in [2, 3, 4]]
    result = vectors.rrf_fuse(lexical, vector, 60, 4)
    rescaled = vectors.rrf_fuse(
        [dict(row, score=1e9) for row in lexical],
        [dict(row, score=1e-9) for row in vector], 60, 4,
    )
    assert [row["id"] for row in result] == [2, 3, 1, 4]
    assert rescaled == result
