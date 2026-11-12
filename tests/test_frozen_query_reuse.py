"""Query rebinds preserve encoder identity; sampled SQL rows never pretend to be measured."""

import hashlib
import json
import sys
from types import ModuleType, SimpleNamespace

import numpy as np
import pytest

from eval import full_corpus_dense as dense
from rag.embedding_inputs import ProfileSpec
from rag.vector_index import Space


def test_split_policy_query_reuse_preserves_exact_vectors_and_original_cost(tmp_path, monkeypatch):
    spec = ProfileSpec(schema_version=1, model_revision="fixed", tokenizer_file="synthetic",
                       tokenizer_sha256="0" * 64, max_tokens=512, pooling="cls", normalization="L2")
    old = SimpleNamespace(spec=spec, signature="before")
    current = SimpleNamespace(spec=spec.model_copy(update={
        "chunking_strategy": "sentence-paragraph-v1"}), signature="after")
    space = Space("synthetic-model", "https://example.invalid", 2, "after", "fixed")
    before = Space(space.model, space.base_url, 2, "before", "fixed")
    monkeypatch.setattr(dense, "load_profile", lambda _: old)
    monkeypatch.setattr(dense, "active_profile", lambda: current)
    monkeypatch.setattr(dense, "space_from_settings", lambda: space)
    monkeypatch.setattr(dense, "_embed_texts", lambda *_: pytest.fail("must not encode queries"))
    data = {"hashes": {"source": "fixed"}, "queries": {"q": {"text": "question"}}}
    source, target = tmp_path / "source", tmp_path / "target"
    source.mkdir()
    np.save(source / "queries.npy", np.array([[1., 0.]], dtype="float32"))
    metadata = {"input_hashes": data["hashes"], "query_ids": ["q"], "space": before.as_dict(),
                "vectors_sha256": hashlib.sha256((source / "queries.npy").read_bytes()).hexdigest(),
                "materialization_seconds": 17.5}
    (source / "queries.metadata.json").write_text(json.dumps(metadata))
    dense.reuse_query_vectors(data, source, target, "old-profile", dimension=2)
    _, vectors = dense.query_vectors(data, target, 2)
    assert vectors.tolist() == [[1., 0.]]
    saved = json.loads((target / "queries.metadata.json").read_text())
    assert saved["materialization_seconds"] == 17.5
    assert saved["reused_query_encoder"]["new_embedding_calls"] == 0
    np.save(target / "queries.npy", np.array([[0., 0.]], dtype="float32"))
    saved["vectors_sha256"] = hashlib.sha256((target / "queries.npy").read_bytes()).hexdigest()
    (target / "queries.metadata.json").write_text(json.dumps(saved))
    with pytest.raises(ValueError, match="norm changed"):
        dense.query_vectors(data, target, 2)


def test_sql_sampling_keeps_unmeasured_rows_null(tmp_path, monkeypatch):
    for name in ["sklearn", "sklearn.metrics", "sklearn.metrics.pairwise"]:
        module = ModuleType(name)
        module.__path__ = []
        monkeypatch.setitem(sys.modules, name, module)
    monkeypatch.setattr(sys.modules["sklearn.metrics.pairwise"], "cosine_similarity",
                        lambda q, _: np.tile([1., .5], (len(q), 1)), raising=False)
    calls = []
    repository = SimpleNamespace(snapshot=lambda: None,
        scores=lambda *args: (calls.append(args) or {0: 1., 1: .5}))
    path = tmp_path / "ranks.jsonl"
    meta = dense.compare_queries(repository, np.eye(2), np.array([0, 1]), ["a", "b"],
        ["q1", "q2", "q3", "q4"], np.ones((4, 2)), path, 2, verify_queries=2)
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    assert len(calls) == meta["postgres_verification_queries"] == 2
    assert not meta["production_complete"]
    assert rows[1]["production"] is rows[2]["production"] is None
    assert rows[0]["production"] == rows[3]["production"] == ["a", "b"]
