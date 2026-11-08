"""Embedding namespace, incremental reuse and complete-publication contracts."""

from dataclasses import replace

import pytest
from _rag_store import memory_store, memory_vectors  # noqa: F401

from config import settings
from rag import vector_index as vectors


@pytest.fixture
def store(monkeypatch, tmp_path):
    for name, value in {
        "embedding_model": "test-model", "embedding_base_url": "https://embedding.invalid/v1",
        "embedding_profile": "", "embedding_dim": 0, "embedding_api_key": "test-only",
        "rag_hybrid_enabled": True,
    }.items():
        monkeypatch.setattr(settings, name, value)
    result = memory_store(tmp_path)
    monkeypatch.setattr(vectors, "_store", result)
    return result


def chunk(identity, text, section="body"):
    return {"type": "note", "id": identity, "section": section, "title": "T", "text": text}


@pytest.mark.parametrize("field,value", [
    ("model", "another"), ("base_url", "https://another.invalid/v1"), ("dim", 1024),
    ("profile", "another-profile"), ("chunking", "another-chunker"),
])
def test_every_embedding_space_dimension_partitions_content_keys(store, field, value):
    space = vectors.space_from_settings()
    assert vectors.chunk_key(space, "same") != vectors.chunk_key(replace(space, **{field: value}),
                                                                  "same")
    assert vectors.chunk_key(space, "same") != vectors.chunk_key(space, "changed")


def test_duplicate_inputs_keep_all_source_slots_without_duplicate_embedding(store, monkeypatch):
    sent = []
    def embed(texts, _):
        sent.extend(texts)
        return [[1, 0] for _ in texts]
    monkeypatch.setattr(vectors, "_embed_texts", embed)
    rows = [chunk(1, "same", "first"), chunk(2, "same", "second")]
    result = store.update(rows, vectors.space_from_settings())
    assert result["embedded"] == 1 and len(sent) == 1 and len(store.view()) == 2
    assert store.view().scores([1, 0], 2) == {0: pytest.approx(1), 1: pytest.approx(1)}
    assert store.update(rows, vectors.space_from_settings())["skipped"] == "aligned"
    assert len(sent) == 1


@pytest.mark.parametrize("bad", [[0, 0], [float("nan"), 1], [float("inf"), 1], [1e-100, 0]])
def test_bad_vectors_never_publish_partial_membership(store, monkeypatch, bad):
    monkeypatch.setattr(vectors, "_embed_texts", lambda texts, _: [[1, 0] for _ in texts])
    assert store.update([chunk(1, "first")], vectors.space_from_settings())["ok"]
    original = store.view().snapshot
    monkeypatch.setattr(vectors, "_embed_texts", lambda texts, _: [bad for _ in texts])
    result = store.update([chunk(1, "first"), chunk(2, "second")], vectors.space_from_settings())
    assert not result["ok"] and result["missing"] == 1
    assert store.view().snapshot == original


def test_unrequested_dimension_change_rebuilds_all_active_inputs_once(store, monkeypatch):
    sent = []
    dimension = [2]
    def embed(texts, _):
        sent.extend(texts)
        return [[1] + [0] * (dimension[0] - 1) for _ in texts]
    monkeypatch.setattr(vectors, "_embed_texts", embed)
    assert store.update([chunk(1, "first")], vectors.space_from_settings())["ok"]
    dimension[0] = 3
    result = store.update([chunk(1, "first"), chunk(2, "second")], vectors.space_from_settings())
    assert result["ok"] and result["embedded"] == 2 and store.view().dim == 3
    assert len(sent) == 4 and result["logical_batches"] == 2
    assert store.update([chunk(1, "first"), chunk(2, "second")],
                        vectors.space_from_settings())["skipped"] == "aligned"


def test_query_dimension_change_invalidates_aligned_shortcut_and_forces_rebuild(store, monkeypatch):
    monkeypatch.setattr(vectors, "_embed_texts", lambda texts, _: [[1, 0] for _ in texts])
    rows = [chunk(1, "first")]
    assert store.update(rows, vectors.space_from_settings())["ok"]
    monkeypatch.setattr(vectors, "_embed_texts", lambda texts, _: [[1, 0, 0] for _ in texts])
    assert vectors.embed_query("new query") is None
    assert store.last_error() == "vector_dimension_changed"
    assert store.update(rows, vectors.space_from_settings())["embedded"] == 1
    assert store.view().dim == 3


def test_background_build_uses_owned_workers_and_shutdown_does_not_publish(store, monkeypatch):
    import asyncio
    import threading

    from agent.runtime.workers import RequestWorkers

    entered, release = threading.Event(), threading.Event()
    monkeypatch.setattr(vectors, "_submit", None)
    monkeypatch.setattr(vectors, "_warm_stop", threading.Event())
    monkeypatch.setattr(vectors, "_warm_lock", threading.Lock())
    def blocking(texts, _):
        entered.set()
        assert release.wait(5)
        return [[1, 0] for _ in texts]
    monkeypatch.setattr(vectors, "_embed_texts", blocking)
    workers = RequestWorkers(max_workers=1)
    vectors.start_background(workers.warm_up)
    try:
        vectors.warm_async([chunk(1, "first")])
        assert entered.wait(5)
        vectors.stop_background()
    finally:
        release.set()
        asyncio.run(workers.aclose())
    assert store.repository.snapshot() is None
    assert not vectors._warm_lock.locked()
    vectors.warm_async([chunk(1, "another")])
    assert store.repository.snapshot() is None


def test_health_route_uses_last_observed_state_without_probing_database(store, monkeypatch):
    monkeypatch.setattr(vectors, "_embed_texts", lambda texts, _: [[1, 0] for _ in texts])
    assert store.update([chunk(1, "first")], vectors.space_from_settings())["ok"]
    def forbidden():
        pytest.fail("cached health diagnostics must not block on PostgreSQL")
    monkeypatch.setattr(store.repository, "snapshot", forbidden)
    assert vectors.route_status()["state"] == "active"
    assert vectors.degraded_reason() is None


def test_queued_background_cancellation_releases_the_build_permit(store, monkeypatch):
    import asyncio
    import threading

    from agent.runtime.workers import RequestWorkers

    monkeypatch.setattr(vectors, "_submit", None)
    monkeypatch.setattr(vectors, "_warm_stop", threading.Event())
    monkeypatch.setattr(vectors, "_warm_lock", threading.Lock())
    workers = RequestWorkers(max_workers=1)
    blocked, release = threading.Event(), threading.Event()
    def blocker():
        blocked.set()
        assert release.wait(5)
    workers.warm_up(blocker)
    submitted = []
    def submit(function):
        future = workers.warm_up(function)
        submitted.append(future)
        return future
    vectors.start_background(submit)
    try:
        assert blocked.wait(5)
        vectors.warm_async([chunk(1, "first")])
        assert vectors._warm_lock.locked()
        assert submitted[0].cancel()
        assert not vectors._warm_lock.locked()
        assert store.repository.snapshot() is None
    finally:
        vectors.stop_background()
        release.set()
        asyncio.run(workers.aclose())


@pytest.mark.parametrize("size", [0, 1, -1])
def test_vector_pool_rejects_capacity_that_cannot_publish_a_locked_build(size):
    from rag.postgres import PostgresRepository

    with pytest.raises(ValueError, match="at least two"):
        PostgresRepository("postgresql://offline.invalid/rag", "test", pool_size=size)
