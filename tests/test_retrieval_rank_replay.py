"""Frozen rank replay retains empty queries and rejects corrupted labels/ranks."""

import csv
import gzip
import hashlib
import json

import pytest

from eval.offline_rag_eval import evaluate
from eval.replay_retrieval_ranks import derived_runs, read_frozen, replay


def fixture(tmp_path):
    labels = b"query-id\tcorpus-id\tscore\nq1\ta\t1\nq2\tb\t1\n"
    with gzip.open(tmp_path / "qrels.tsv.gz", "wb") as stream:
        stream.write(labels)
    names = ["bm25", "bm25_raw", "bm25_whole_document", "bm25_jieba_whole_document",
             "vector", "vector_reference"]
    with gzip.open(tmp_path / "ranks.csv.gz", "wt", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(["query_id", "arm", "rank", "document_id"])
        writer.writerows(["q2", name, 1, "b"] for name in names)
    ranked = {name: {"q1": {}, "q2": {"b": -1.}} for name in names}
    report = {"dataset": {}, "input_hashes": {
        "qrels/dev.tsv": hashlib.sha256(labels).hexdigest()}, "frozen_run_storage": {
        "qrels": "qrels.tsv.gz", "ranks": "ranks.csv.gz", "primitive_arms": names,
        "metric_cutoffs": [1, 8, 100], "early_chunk_prefix_counts": {
            "q1": {"bm25_raw": 0, "vector_reference": 0},
            "q2": {"bm25_raw": 1, "vector_reference": 1}}},
        "group_query_ids": {"both": ["q1", "q2"]}, "group_queries": {"both": 2}}
    derived_runs(report, ranked)
    data = {"queries": {"q1": {}, "q2": {}}, "qrels": {"q1": {"a": 1}, "q2": {"b": 1}}}
    report["arms"] = {name: evaluate(data, run, [1, 8, 100]) for name, run in ranked.items()}
    report["groups"] = {name: {"both": {"Success@1": .5}} for name in ranked}
    path = tmp_path / "summary.json.gz"
    with gzip.open(path, "wt") as stream:
        json.dump(report, stream)
    return path


def test_frozen_replay_counts_empty_queries_and_uses_no_model(tmp_path):
    result = replay(fixture(tmp_path))
    assert result["queries"] == 2 and result["arms"] == 11
    assert result["model_calls"] == result["database_calls"] == 0


def test_frozen_replay_rejects_changed_qrels_and_duplicate_rank(tmp_path):
    path = fixture(tmp_path)
    with gzip.open(tmp_path / "ranks.csv.gz", "rt") as stream:
        rows = list(csv.reader(stream))
    with gzip.open(tmp_path / "ranks.csv.gz", "wt", newline="") as stream:
        csv.writer(stream).writerows([*rows, rows[-1]])
    with pytest.raises(ValueError, match="duplicate"):
        read_frozen(path)
    with gzip.open(tmp_path / "qrels.tsv.gz", "wb") as stream:
        stream.write(b"changed")
    with pytest.raises(ValueError, match="changed frozen qrels"):
        read_frozen(path)
