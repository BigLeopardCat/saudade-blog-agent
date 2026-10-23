"""Operator plans cannot spend API calls; execution is bounded and incremental."""

import json

import pytest

from config import settings
from rag import manage, search
from rag import vector_index as vectors
from rag.corpus import CorpusUnavailable


@pytest.fixture
def operations(monkeypatch, tmp_path):
    for name, value in {
        "embedding_model": "fixture",
        "embedding_api_key": "fixture-key",
        "embedding_base_url": "https://embedding.invalid/v1",
        "embedding_profile": "",
        "embedding_dim": 2,
        "rag_hybrid_enabled": True,
        "rag_vector_dir": str(tmp_path),
    }.items():
        monkeypatch.setattr(settings, name, value)
    monkeypatch.setattr(vectors, "_store", vectors.VectorStore(tmp_path))
    monkeypatch.setattr(vectors, "_query_cache", {})
    monkeypatch.setattr(vectors, "_query_order", [])
    sent = []

    def embed(texts, space):
        sent.extend(texts)
        return [[1.0] + [0.0] * ((space.dim or 2) - 1) for _ in texts]

    monkeypatch.setattr(vectors, "_embed_texts", embed)
    monkeypatch.setattr(
        search.RagIndex,
        "_fetch_corpus",
        lambda _: [
            {"type": "note", "id": 1, "title": "博客索引", "content": "更新博客的索引正文"},
        ],
    )

    def forbidden(_):
        pytest.fail("operator command started implicit background indexing")

    monkeypatch.setattr(search, "warm_vectors", forbidden)
    return sent


def test_plan_execute_repeat_and_query(operations, capsys):
    assert manage.main(["prepare"]) == 0
    assert json.loads(capsys.readouterr().out)["new_or_retry_inputs"] == 1
    assert operations == []
    assert manage.main(["prepare", "--execute", "--max-new", "0"]) == 1
    assert operations == []
    capsys.readouterr()
    assert manage.main(["prepare", "--execute", "--max-new", "1"]) == 0
    assert len(operations) == 1
    capsys.readouterr()
    assert manage.main(["prepare", "--execute", "--max-new", "0"]) == 0
    assert json.loads(capsys.readouterr().out)["new_or_retry_inputs"] == 0
    assert len(operations) == 1
    assert manage.main(["status", "--require-vectors"]) == 0
    capsys.readouterr()
    assert manage.main(["query", "博客索引", "--require-hybrid"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["route"]["mode"] == "hybrid" and result["hits"][0]["id"] == 1
    assert len(operations) == 2  # Only one additional query embedding.


def test_query_without_vectors_does_not_build_them(operations, capsys):
    assert manage.main(["query", "博客索引", "--require-hybrid"]) == 1
    assert json.loads(capsys.readouterr().out)["route"]["mode"] == "degraded"
    assert operations == []


def test_source_outage_cannot_publish_empty_index(operations, monkeypatch, capsys):
    def unavailable(_):
        raise CorpusUnavailable("private endpoint or secret must not be printed")

    monkeypatch.setattr(search.RagIndex, "_fetch_corpus", unavailable)
    assert manage.main(["prepare", "--execute"]) == 1
    assert json.loads(capsys.readouterr().out) == {
        "ok": False,
        "error_class": "CorpusUnavailable",
    }
    assert operations == [] and not vectors.get_store().manifest_path.exists()


def test_probe_checks_shape_and_nonzero(operations, monkeypatch):
    assert manage.probe(2)["ok"]
    assert not manage.probe(3)["ok"]
    monkeypatch.setattr(vectors, "_embed_texts", lambda texts, _: [[0, 0] for _ in texts])
    assert not manage.probe(2)["ok"]


def test_private_env_file_is_loaded(operations, tmp_path, capsys):
    path = tmp_path / "private.env"
    path.write_text("EMBEDDING_MODEL=from-private-env\n")
    assert manage.main(["--env-file", str(path), "status"]) == 0
    assert json.loads(capsys.readouterr().out)["model"] == "from-private-env"


@pytest.mark.parametrize("value", [[0, 0], [1, float("nan")], [1, float("inf")], [1e-100, 0]])
def test_invalid_vectors_are_rejected_for_document_and_query(operations, monkeypatch, value):
    monkeypatch.setattr(vectors, "_embed_texts", lambda texts, _: [value for _ in texts])
    store = vectors.get_store()
    chunks = [{"type": "note", "id": 1, "title": "T", "section": "s", "text": "body"}]
    assert not store.update(chunks, vectors.space_from_settings())["ok"]
    assert store.view() is None and vectors.embed_query("q") is None


def test_requested_dimension_is_enforced_without_existing_index(operations, monkeypatch):
    monkeypatch.setattr(vectors, "_embed_texts", lambda *_: [[1, 0, 0]])
    assert vectors.embed_query("q") is None


def test_switch_native_dimensions_embeds_each_document_once(operations, monkeypatch):
    monkeypatch.setattr(settings, "embedding_dim", 0)
    chunks = [{"type": "note", "id": 1, "title": "T", "section": "s", "text": "body"}]
    store = vectors.get_store()
    assert store.update(chunks, vectors.space_from_settings())["ok"]
    calls = []

    def larger(texts, _):
        calls.extend(texts)
        return [[1, 0, 0] for _ in texts]

    monkeypatch.setattr(settings, "embedding_model", "larger-model")
    monkeypatch.setattr(vectors, "_embed_texts", larger)
    assert store.update(chunks, vectors.space_from_settings())["ok"]
    assert len(calls) == 1 and store.view().dim == 3


def test_missing_private_env_file_cannot_fall_back_to_default(operations, tmp_path, capsys):
    assert (
        manage.main(["--env-file", str(tmp_path / "missing.env"), "probe", "--expected-dim", "2"])
        == 1
    )
    assert json.loads(capsys.readouterr().out)["error_class"] == "ValueError"
    assert operations == []
