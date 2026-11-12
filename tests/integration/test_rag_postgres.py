"""pgvector exact ranking, atomic publication and isolated runtime permissions."""

import os
import threading
import uuid

import pytest
from sqlalchemy import delete, event, select, text
from sqlalchemy.exc import DBAPIError, IntegrityError

from config import settings
from rag import vector_index as vectors
from rag.postgres import PostgresRepository, StaleSnapshot, embeddings, members, snapshots

pytestmark = pytest.mark.enable_socket


@pytest.fixture
def database(monkeypatch):
    dsn = os.environ.get("SAUDADE_RAG_TEST_DSN")
    if not dsn:
        pytest.skip("requires explicit disposable SAUDADE_RAG_TEST_DSN")
    repository = PostgresRepository(dsn, "test-" + uuid.uuid4().hex)
    repository.setup()
    for name, value in {
        "rag_vector_dsn": dsn, "rag_vector_collection": repository.collection,
        "rag_hybrid_enabled": True, "embedding_model": "test-model", "embedding_dim": 2,
        "embedding_profile": "", "embedding_base_url": "https://fixture.invalid/v1",
        "embedding_api_key": "test-only",
    }.items():
        monkeypatch.setattr(settings, name, value)
    store = vectors.VectorStore(repository)
    monkeypatch.setattr(vectors, "_store", store)
    monkeypatch.setattr(vectors, "_query_cache", {})
    monkeypatch.setattr(vectors, "_query_order", [])
    calls = []

    def embed(texts, _):
        calls.extend(texts)
        return [[1.0, 0.0] if "first" in value else [0.8, 0.6] for value in texts]

    monkeypatch.setattr(vectors, "_embed_texts", embed)
    try:
        yield store, calls
    finally:
        with repository.engine.begin() as connection:
            for table in (members, snapshots, embeddings):
                connection.execute(delete(table).where(table.c.collection == repository.collection))
        repository.close()


def chunks(*bodies):
    return [{"type": "note", "id": index, "title": str(index), "section": "body", "text": body}
            for index, body in enumerate(bodies, 1)]


def test_large_publication_prunes_obsolete_keys_and_dimensions_within_database_budget(database):
    store, _ = database
    repository = store.repository
    count = 20000
    keys = [f"scale-{index}" for index in range(count)]
    source = [{"type": "note", "id": index} for index in range(count)]
    common = {"space": {"model": "scale-fixture"}, "space_key": "scale-fixture",
              "dimension": 2, "stats": {}, "complete": True}
    repository.commit(rows=[{"key": key, "dimension": 2, "embedding": [1.0, 0.0]}
                            for key in keys], chunks=source, keys=keys,
                      fingerprint="large", **common)
    assert repository.snapshot().count == count
    keep = count // 2
    repository.commit(rows=[{"key": keys[0], "dimension": 3, "embedding": [1.0, 0.0, 0.0]}],
                      chunks=source[:keep], keys=keys[:keep], fingerprint="pruned", **common)
    assert repository.snapshot().count == keep
    with repository.engine.connect() as connection:
        retained = connection.execute(select(embeddings.c.key, embeddings.c.dimension).where(
            embeddings.c.collection == repository.collection)).all()
    assert set(retained) == {(key, 2) for key in keys[:keep]}


def test_database_cosine_ranks_documents_instead_of_truncating_chunks(database, monkeypatch):
    store, _ = database
    rows = [{"type": "note", "id": 1, "title": "long", "section": str(i),
             "text": "first " + str(i)} for i in range(20)] + chunks("second", "third")
    rows[-2]["id"], rows[-1]["id"] = 2, 3
    assert store.update(rows, vectors.space_from_settings())["ok"]
    view = store.view()
    scores = view.scores([9.0, 0.0], 2)
    assert set(scores) == set(range(21))
    assert scores[0] == pytest.approx(1.0)
    assert scores[20] == pytest.approx(0.8)
    assert view.scores([9.0, 0.0], 1, article_id=2) == {20: pytest.approx(0.8)}
    assert view.scores([9.0, 0.0], 1, article_id=999) == {}
    # Results come from the pgvector distance operator, without reading vectors into Python.
    statements = []
    def record(conn, cursor, statement, *args):
        statements.append(statement)
    event.listen(store.repository.engine, "before_cursor_execute", record)
    try:
        assert view.scores([0.0, 2.0], 1) == {20: pytest.approx(0.6)}
        assert any("<=>" in statement for statement in statements)
        assert not any("SELECT rag.embeddings.embedding" in statement for statement in statements)
    finally:
        event.remove(store.repository.engine, "before_cursor_execute", record)


@pytest.mark.parametrize("stage", ["embeddings", "members", "snapshots"])
def test_interrupted_transaction_preserves_old_complete_snapshot(database, stage):
    store, _ = database
    original = chunks("first", "second")
    space = vectors.space_from_settings()
    assert store.update(original, space)["ok"]
    view = store.view()
    before = view.scores([1, 0], 2)

    def interrupt(conn, cursor, statement, *args):
        if statement.startswith("INSERT INTO rag." + stage):
            raise OSError("synthetic transaction interruption")

    event.listen(store.repository.engine, "before_cursor_execute", interrupt)
    try:
        with pytest.raises(OSError):
            store.update(chunks("changed first", "changed second"), space)
    finally:
        event.remove(store.repository.engine, "before_cursor_execute", interrupt)
    reader = vectors.VectorStore(store.repository)
    assert reader.load() and reader.view().snapshot.fingerprint == view.snapshot.fingerprint
    assert view.scores([1, 0], 2) == before
    assert reader.view_for(vectors.corpus_fingerprint(space, chunks("changed first"))) is None


def test_restart_incremental_reuse_and_deleted_public_document_purge(database):
    store, calls = database
    original = chunks("first", "second")
    space = vectors.space_from_settings()
    assert store.update(original, space)["embedded"] == 2
    old_view = store.view()
    repository = PostgresRepository(settings.rag_vector_dsn, settings.rag_vector_collection)
    try:
        reader = vectors.VectorStore(repository)
        assert reader.load()
        assert reader.view().scores([1, 0], 2) == old_view.scores([1, 0], 2)
        assert reader.update(original, space)["skipped"] == "aligned" and len(calls) == 2
        assert reader.update(chunks("first", "changed second"), space)["embedded"] == 1
        assert len(calls) == 3
        assert reader.update(chunks("first"), space)["embedded"] == 0
        with repository.engine.connect() as connection:
            assert len(connection.execute(select(embeddings).where(
                embeddings.c.collection == repository.collection)).all()) == 1
        with pytest.raises(StaleSnapshot):
            old_view.scores([1, 0], 2)
    finally:
        repository.close()


def test_partial_embeddings_are_reused_but_cannot_publish_an_incomplete_corpus(database,
                                                                             monkeypatch):
    store, calls = database
    space = vectors.space_from_settings()
    assert store.update(chunks("first"), space)["ok"]
    original = store.view()
    monkeypatch.setattr(vectors, "_embed_texts", lambda texts, _: [[0, 1], None])
    failed = store.update(chunks("first", "new second", "third"), space)
    assert failed["ok"] is False and failed["missing"] == 1
    assert store.view().snapshot == original.snapshot
    sent = []
    def embed(texts, _):
        sent.extend(texts)
        return [[1, 0] for _ in texts]
    monkeypatch.setattr(vectors, "_embed_texts", embed)
    completed = store.update(chunks("first", "new second", "third"), space)
    assert completed["ok"] and completed["embedded"] == 1 and completed["reused"] == 2
    assert len(sent) == 1 and "third" in sent[0] and len(calls) == 1


def test_foreign_key_and_dimension_constraints_rollback_publication(database):
    store, _ = database
    space = vectors.space_from_settings()
    assert store.update(chunks("first"), space)["ok"]
    before = store.repository.snapshot()
    with pytest.raises(IntegrityError):
        store.repository.commit(
            rows=[], chunks=chunks("missing"), keys=["nonexistent"], space=space.as_dict(),
            space_key="unused", fingerprint="invalid", dimension=2, stats={}, complete=True)
    assert store.repository.snapshot() == before
    with pytest.raises(DBAPIError):
        with store.repository.engine.begin() as connection:
            connection.execute(embeddings.insert(), {
                "collection": store.repository.collection, "key": "bad", "dimension": 3,
                "space": "unused", "embedding": [1, 0]})
    assert store.repository.snapshot() == before


def test_writer_has_no_open_transaction_while_model_io_and_readers_keep_old_data(database,
                                                                               monkeypatch):
    store, _ = database
    space = vectors.space_from_settings()
    assert store.update(chunks("first"), space)["ok"]
    view = store.view()
    entered, release = threading.Event(), threading.Event()
    def blocking(texts, _):
        entered.set()
        assert release.wait(5)
        return [[0, 1] for _ in texts]
    monkeypatch.setattr(vectors, "_embed_texts", blocking)
    results = []
    thread = threading.Thread(target=lambda: results.append(store.update(chunks("second"), space)))
    thread.start()
    try:
        assert entered.wait(5)
        assert view.scores([1, 0], 1) == {0: pytest.approx(1)}
        with store.repository.writer() as acquired:
            assert not acquired
        with store.repository.engine.connect() as connection:
            assert connection.scalar(text(
                "SELECT count(*) FROM pg_stat_activity WHERE datname=current_database() "
                "AND state='idle in transaction' AND pid <> pg_backend_pid()")) == 0
    finally:
        release.set()
        thread.join(5)
    assert not thread.is_alive() and results[0]["ok"]
    with pytest.raises(StaleSnapshot):
        view.scores([1, 0], 1)


def test_embedding_space_and_collection_are_isolated(database, monkeypatch):
    store, calls = database
    rows = chunks("first")
    assert store.update(rows, vectors.space_from_settings())["ok"]
    original = store.view()
    other = PostgresRepository(settings.rag_vector_dsn, "test-" + uuid.uuid4().hex)
    try:
        assert other.snapshot() is None
        assert other.available("unknown") == {}
        monkeypatch.setattr(settings, "embedding_model", "another-model")
        assert store.view() is None
        assert store.update(rows, vectors.space_from_settings())["embedded"] == 1
        assert len(calls) == 2
        with pytest.raises(StaleSnapshot):
            original.scores([1, 0], 1)
        assert other.snapshot() is None
        with store.repository.engine.connect() as connection:
            assert len(connection.execute(select(embeddings).where(
                embeddings.c.collection == store.repository.collection)).all()) == 1
    finally:
        other.close()


def test_nonunit_opposite_and_random_vectors_match_reference_library(database, monkeypatch):
    import numpy as np
    from scipy.spatial.distance import cdist

    store, _ = database
    source = np.random.default_rng(20261108).normal(size=(23, 2))
    source[0], source[1] = [2, 0], [-4, 0]
    monkeypatch.setattr(vectors, "_embed_texts", lambda texts, _: source.tolist())
    assert store.update(chunks(*(str(i) for i in range(len(source)))),
                        vectors.space_from_settings())["ok"]
    for query in ([9, 0], [0, 3], [-1, 4]):
        expected = 1 - cdist([query], source, metric="cosine")[0]
        actual = store.view().scores(query, len(source))
        assert actual == {i: pytest.approx(score, abs=1e-6)
                          for i, score in enumerate(expected) if score > 0}
    with pytest.raises(ValueError):
        store.view().scores([1, 0, 0], 1)


def test_runtime_role_can_write_and_search_without_schema_or_extension_ownership(database):
    store, _ = database
    administrator = store.repository
    role = "rag_role_" + uuid.uuid4().hex
    password = uuid.uuid4().hex
    with administrator.engine.begin() as connection:
        connection.execute(text(f"CREATE ROLE {role} LOGIN PASSWORD '{password}'"))
        connection.execute(text(f"GRANT USAGE ON SCHEMA rag TO {role}"))
        connection.execute(text(f"GRANT SELECT, INSERT, UPDATE, DELETE "
                                f"ON ALL TABLES IN SCHEMA rag TO {role}"))
    runtime = PostgresRepository(str(administrator.engine.url.set(
        username=role, password=password).render_as_string(hide_password=False)),
        administrator.collection)
    try:
        process = vectors.VectorStore(runtime)
        assert process.update(chunks("first"), vectors.space_from_settings())["ok"]
        assert process.view().scores([1, 0], 1) == {0: pytest.approx(1)}
        with pytest.raises(DBAPIError):
            with runtime.engine.begin() as connection:
                connection.execute(text("CREATE TABLE rag.forbidden_runtime_ddl(id integer)"))
        with runtime.engine.connect() as connection:
            assert connection.scalar(text("SELECT current_user")) == role
    finally:
        runtime.close()
        with administrator.engine.begin() as connection:
            connection.execute(text(f"DROP OWNED BY {role}"))
            connection.execute(text(f"DROP ROLE {role}"))


def test_late_old_source_cannot_be_mixed_with_a_new_corpus_fingerprint(database):
    store, _ = database
    space = vectors.space_from_settings()
    old, current = chunks("first"), chunks("second")
    assert store.update(old, space)["ok"]
    assert store.update(current, space)["ok"]
    current_view = store.view()
    # Source hashes have no total order. Last publication is atomic but not a freshness fence.
    assert store.update(old, space)["ok"]
    assert store.view_for(vectors.corpus_fingerprint(space, current)) is None
    with pytest.raises(StaleSnapshot):
        current_view.scores([1, 0], 1)


def test_incomplete_membership_is_unavailable_rather_than_negative_evidence(database):
    store, _ = database
    assert store.update(chunks("first", "second"), vectors.space_from_settings())["ok"]
    with store.repository.engine.begin() as connection:
        connection.execute(delete(members).where(
            members.c.collection == store.repository.collection, members.c.ordinal == 0))
    assert not store.load()
    assert store.view() is None and store.last_error() == "vector_database_unavailable"


def test_database_failure_falls_back_to_lexical_without_claiming_semantic_coverage(database):
    from sqlalchemy.exc import OperationalError

    from rag.search import RagIndex, last_route

    store, _ = database
    documents = [{"type": "note", "id": 1, "title": "first", "content": "searchable body"}]
    index = RagIndex(corpus_loader=lambda: documents, vector_warmer=lambda _: None)
    index.build()
    assert store.update(index._chunks, vectors.space_from_settings())["ok"]
    def unavailable(conn, cursor, statement, *args):
        raise OperationalError("sensitive-database-diagnostic", {},
                               RuntimeError("synthetic outage"))
    event.listen(store.repository.engine, "before_cursor_execute", unavailable)
    try:
        assert index.search("searchable", 8)[0]["id"] == 1
        assert last_route()["mode"] == "degraded"
        assert last_route()["reason"] == "vector_database_unavailable"
    finally:
        event.remove(store.repository.engine, "before_cursor_execute", unavailable)


@pytest.mark.parametrize("change", ["delete_last", "make_all_private"])
def test_empty_public_corpus_clears_members_and_vectors_without_embedding(database, change):
    from rag.search import RagIndex, last_route

    store, calls = database
    public = [{"type": "note", "id": 1, "title": "first", "content": "public body",
               "visible": True}]
    index = RagIndex(corpus_loader=lambda: [row for row in public if row["visible"]],
                     vector_warmer=lambda _: None)
    index.build()
    space = vectors.space_from_settings()
    assert store.update(index._chunks, space)["embedded"] == 1
    previous_calls = list(calls)
    # The authoritative public export excludes both deleted and newly private documents.
    if change == "delete_last":
        public.clear()
    else:
        public[0]["visible"] = False
    index.build()
    result = store.update(index._chunks, space)
    assert result["ok"] and result["embedded"] == result["logical_batches"] == 0
    assert result["dim"] == 0 and calls == previous_calls
    snapshot = store.repository.snapshot()
    assert snapshot is not None and snapshot.count == snapshot.dimension == 0
    assert snapshot.fingerprint == vectors.corpus_fingerprint(space, [])
    with store.repository.engine.connect() as connection:
        for table in (members, embeddings):
            assert connection.execute(select(table).where(
                table.c.collection == store.repository.collection)).all() == []
    assert index.search("public body", 8) == []
    assert last_route()["reason"] == "no_vector_hits" and calls == previous_calls
    reader = vectors.VectorStore(store.repository)
    assert reader.load() and reader.view_for(snapshot.fingerprint) is not None
    assert len(reader.view()) == 0 and reader.view().scores([], 8) == {}


def test_cold_empty_corpus_publishes_a_zero_dimension_snapshot(database, monkeypatch):
    store, calls = database
    monkeypatch.setattr(settings, "embedding_dim", 0)
    assert store.repository.snapshot() is None
    result = store.update([], vectors.space_from_settings())
    assert result["ok"] and result["dim"] == 0 and result["embedded"] == 0
    assert result["api_calls"] == result["logical_batches"] == 0 and calls == []
    snapshot = store.repository.snapshot()
    assert snapshot is not None and snapshot.count == snapshot.dimension == 0
    assert store.update([], vectors.space_from_settings())["skipped"] == "aligned"


def test_minimal_supported_pool_can_build_and_search(database):
    store, _ = database
    repository = PostgresRepository(settings.rag_vector_dsn, store.repository.collection,
                                    pool_size=2)
    try:
        process = vectors.VectorStore(repository)
        assert process.update(chunks("first"), vectors.space_from_settings())["ok"]
        assert process.view().scores([1, 0], 1) == {0: pytest.approx(1)}
    finally:
        repository.close()
