"""Reference controls keep corpus coverage, candidate budgets and source bindings explicit."""

import hashlib
import json
from types import SimpleNamespace

import numpy as np
import pytest

from eval import full_corpus_dense as dense
from eval.full_corpus_analysis import fusion, read_stage, source_groups
from eval.full_corpus_lexical import document_ranks, execute


def test_frozen_query_vectors_match_application_whitespace_and_do_not_repeat_model_calls(
        tmp_path, monkeypatch):
    sent = []
    monkeypatch.setattr(dense, "active_profile", lambda: None)
    monkeypatch.setattr(dense, "space_from_settings", lambda: SimpleNamespace(as_dict=lambda: {}))
    monkeypatch.setattr(dense, "_embed_texts", lambda texts, _: (
        sent.extend(texts) or [[1.] + [0.] * 511 for _ in texts]))
    data = {"hashes": {"source": "fixed"}, "queries": {
        "q": {"text": " \t实体问题\n "}, "duplicate": {"text": "实体问题"}}}
    qids, values = dense.query_vectors(data, tmp_path)
    assert qids == ["q", "duplicate"] and sent == ["实体问题"] and values.shape == (2, 512)
    dense.query_vectors(data, tmp_path)
    assert sent == ["实体问题"]
    with (tmp_path / "queries.npy").open("ab") as stream:
        stream.write(b"changed")
    with pytest.raises(ValueError, match="binding changed"):
        dense.query_vectors(data, tmp_path)


def test_document_budget_is_applied_after_chunk_aggregation():
    scores = np.array([.99, .98, .97, .96, .95])
    owners = np.array([0, 0, 0, 1, 2])
    assert document_ranks(scores, owners, 3, 3) == [0, 1, 2]
    assert list(dict.fromkeys(owners[:3])) == [0]
    assert document_ranks(np.array([0., -.1, 0.]), np.arange(3), 3, 3) == []


def test_length_strata_use_positive_labels_and_keep_the_original_query_population():
    data = {"corpus": {"long": {"text": "x" * 2000}, "short": {"text": "short"}},
            "queries": {"a": {}, "b": {}},
            "qrels": {"a": {"long": 2}, "b": {"long": 0, "short": 1}}}
    order, groups = source_groups(data, {"long": 3, "short": 1})
    assert set(order) == {"a", "b"}
    assert groups["has_positive_source_over_1800_chars"] == ["a"]
    assert groups["has_positive_source_with_multiple_vector_chunks"] == ["a"]


def test_native_fusion_and_application_agree_without_dropping_one_route():
    assert fusion(["a", "b", "c"], ["b", "d", "a"], 2) == ["b", "a", "d"]
    assert fusion([], ["b", "a"], 2) == ["b", "a"]


def test_stage_rejects_changed_data_and_incomplete_queries(tmp_path):
    path = tmp_path / "stage.jsonl"
    path.write_text(json.dumps({"query_id": "q", "reference": ["a"]}) + "\n")
    meta = {"input_hashes": {"corpus": "fixed"},
            "output_sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
    path.with_suffix(".metadata.json").write_text(json.dumps(meta))
    data = {"hashes": meta["input_hashes"], "queries": {"q": {}}}
    assert read_stage(path, data)[0]["q"]["reference"] == ["a"]
    with pytest.raises(ValueError, match="incomplete"):
        read_stage(path, {**data, "queries": {"q": {}, "missing": {}}})
    with pytest.raises(ValueError, match="hash mismatch"):
        read_stage(path, {**data, "hashes": {"corpus": "changed"}})


@pytest.mark.parametrize("mode,tokenizer", [
    ("document", "application"), ("chunk", "application"), ("document", "jieba")])
def test_native_and_application_lexical_routes_preserve_source_labels(tmp_path, mode, tokenizer):
    fixture = tmp_path / "data"
    (fixture / "qrels").mkdir(parents=True)
    (fixture / "manifest.json").write_text(json.dumps({"schema_version": 1, "name": "fixture",
        "source": "synthetic", "revision": "fixed", "license": "synthetic", "scope": "test"}))
    corpus = [{"_id": "a", "text": "## 函数\nPython asyncio Future 表示异步结果。"},
              {"_id": "b", "text": "## 背景\n花朵阳光\n## 海洋\n风浪"}]
    (fixture / "corpus.jsonl").write_text("\n".join(json.dumps(r) for r in corpus) + "\n")
    (fixture / "queries.jsonl").write_text(
        json.dumps({"_id": "q", "text": "asyncio Future"}) + "\n")
    (fixture / "qrels/dev.tsv").write_text("query-id\tcorpus-id\tscore\nq\ta\t2\n")
    output = tmp_path / "run.jsonl"
    execute(fixture, mode, output, 2, tokenizer)
    result = json.loads(output.read_text())
    assert result["reference"] == result["raw_application"] == result["production"] == ["a"]
    metadata = json.loads(output.with_suffix(".metadata.json").read_text())
    assert metadata["backend"]["tokenizer"]["name"] == (
        "jieba-cut-for-search" if tokenizer == "jieba" else "bilingual-search-v1")
