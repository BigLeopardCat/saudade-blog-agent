"""Evidence and refresh behavior under hybrid retrieval and source outages."""

import threading
import time

import pytest

from rag.ranking import documents
from rag.search import REFRESH_TTL, RagIndex


def row(section, offset):
    return {"type": "note", "id": 1, "title": "标题", "corpus_revision": "corpus",
            "document_revision": "document", "sections": [section], "section_ids": [section],
            "matches": [{"section_id": section, "offset": offset, "end": offset + 10}]}


def test_chunk_aggregation_keeps_locations_aligned_with_repeated_sections():
    chunks = [{"type": "note", "id": 1, "title": "T", "section": label,
               "section_id": section, "chunk_id": str(i), "start": i * 100, "end": i * 100 + 50}
              for i, (section, label) in enumerate([("s1", "重复"), ("s1", "重复"),
                                                   ("s2", "另节"), ("s3", "重复")])]
    ranked = documents(chunks, {0: 4, 1: 3, 2: 2, 3: 1}, "rev")[0]
    assert ranked["section_ids"] == ["s1", "s2", "s3"]
    assert [m["offset"] for m in ranked["matches"]] == [0, 200, 300]


@pytest.mark.parametrize("vector_available", [False, True])
def test_candidate_pool_is_wider_than_output_and_fallback_remains_bounded(monkeypatch,
                                                                         vector_available):
    index = RagIndex()
    calls = []
    candidates = [{**row("s1", 0), "id": i} for i in range(20)]

    def lexical(query, count, *, article_id=None):
        calls.append(("lexical", count, article_id))
        return candidates[:count]

    def vector(query, count, *, article_id=None):
        calls.append(("vector", count, article_id))
        return ([candidates[10]] if vector_available else []), None, 0

    monkeypatch.setattr(index, "_lexical_ranked", lexical)
    monkeypatch.setattr(index, "_vector_ranked", vector)
    result = index.search("test", top_k=2, article_id=10)
    assert calls == [("lexical", 20, 10), ("vector", 20, 10)]
    assert len(result) == 2
    # The overlap below lexical top_k must reach document fusion.
    assert result[0]["id"] == (10 if vector_available else 0)


def test_stale_queries_serve_snapshot_while_one_background_refresh_runs(monkeypatch):
    entered, release, done = threading.Event(), threading.Event(), threading.Event()
    calls = []
    source = [{"type": "note", "id": 1, "title": "needle", "content": "needle"}]
    index = RagIndex(corpus_loader=lambda: source, vector_warmer=lambda chunks: None)
    index.build()
    index._last_build = time.time() - REFRESH_TTL - 1
    index._last_attempt = 0

    def slow_source():
        calls.append(True)
        entered.set()
        assert release.wait(5)
        raise RuntimeError("source down")

    monkeypatch.setattr(index, "_corpus_loader", slow_source)
    original = index.build

    def build():
        try:
            return original()
        finally:
            done.set()

    monkeypatch.setattr(index, "build", build)
    try:
        assert index.search("needle")[0]["id"] == 1
        assert entered.wait(2)
        for _ in range(5):
            assert index.search("needle")[0]["id"] == 1
        assert calls == [True]
        index._last_build = time.time() - REFRESH_TTL * 2 - 1
        assert index.search("needle") is None
    finally:
        release.set()
        assert done.wait(2)
    assert index.status()["error"] == "RuntimeError"
    assert index.search("needle") is None
    assert calls == [True]  # Failed refresh backoff, even after grace expiry.


@pytest.mark.parametrize("top_k", [True, 0, -1, 51, 1.5])
def test_invalid_count_does_not_start_retrieval(monkeypatch, top_k):
    index = RagIndex()
    monkeypatch.setattr(index, "build", lambda: pytest.fail("must not fetch corpus"))
    with pytest.raises(ValueError, match="top_k"):
        index.search("needle", top_k=top_k)


def test_background_thread_start_failure_does_not_block_later_refresh(monkeypatch):
    index = RagIndex()
    def failed_start(self):
        raise RuntimeError("cannot start thread")
    monkeypatch.setattr(threading.Thread, "start", failed_start)
    with pytest.raises(RuntimeError, match="cannot start"):
        index.refresh_async()
    assert not index._refresh_lock.locked()
