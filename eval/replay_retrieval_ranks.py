"""Recompute frozen retrieval metrics from standard gzip CSV ranks and BEIR qrels; no models."""

from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import io
import json
import math
from pathlib import Path

from eval.full_corpus_analysis import fusion
from eval.offline_rag_eval import evaluate


def scalar_metrics(actual, expected, label):
    if not expected.keys() <= actual.keys() or any(not math.isclose(
            actual[key], value, rel_tol=1e-12, abs_tol=1e-12) for key, value in expected.items()):
        raise ValueError("frozen metric mismatch: " + label)


def read_frozen(summary):
    with gzip.open(summary, "rt", encoding="utf-8") as stream:
        report = json.load(stream)
    storage = report["frozen_run_storage"]
    with gzip.open(summary.parent / storage["qrels"], "rb") as stream:
        raw = stream.read()
    expected = report["input_hashes"]["qrels/dev.tsv"]
    if hashlib.sha256(raw).hexdigest() != expected:
        raise ValueError("changed frozen qrels")
    qrels = {}
    reader = csv.DictReader(io.StringIO(raw.decode()), delimiter="\t")
    if reader.fieldnames != ["query-id", "corpus-id", "score"]:
        raise ValueError("expected BEIR qrels header")
    for row in reader:
        labels = qrels.setdefault(row["query-id"], {})
        if row["corpus-id"] in labels:
            raise ValueError("duplicate frozen judgment")
        labels[row["corpus-id"]] = int(row["score"])
    ranked = {arm: {qid: {} for qid in qrels} for arm in storage["primitive_arms"]}
    seen = {arm: {qid: set() for qid in qrels} for arm in ranked}
    with gzip.open(summary.parent / storage["ranks"], "rt", encoding="utf-8", newline="") as stream:
        reader = csv.DictReader(stream)
        if reader.fieldnames != ["query_id", "arm", "rank", "document_id"]:
            raise ValueError("expected frozen rank CSV header")
        for row in reader:
            arm, qid, did, rank = row["arm"], row["query_id"], row["document_id"], int(row["rank"])
            if (arm not in ranked or qid not in qrels or rank < 1
                    or rank in seen[arm][qid] or did in ranked[arm][qid]):
                raise ValueError("invalid or duplicate frozen rank")
            ranked[arm][qid][did] = -float(rank)
            seen[arm][qid].add(rank)
    for arm in ranked:
        for qid, hits in ranked[arm].items():
            if seen[arm][qid] != set(range(1, len(hits) + 1)):
                raise ValueError("noncontiguous frozen ranks")
            ranked[arm][qid] = dict(sorted(hits.items(), key=lambda row: -row[1]))
    return report, qrels, ranked


def derived_runs(report, ranked):
    counts = report["frozen_run_storage"]["early_chunk_prefix_counts"]
    for arm, source in (("bm25_chunk_first100", "bm25_raw"),
                        ("vector_chunk_first100", "vector_reference")):
        ranked[arm] = {qid: {did: -float(i + 1) for i, did in enumerate(
            list(hits)[:counts[qid][source]])} for qid, hits in ranked[source].items()}
    for pool in (24, 50, 100):
        ranked[f"hybrid{pool}"] = {qid: {did: -float(i + 1) for i, did in enumerate(fusion(
            list(ranked["bm25"][qid]), list(ranked["vector"][qid]), pool))}
            for qid in ranked["bm25"]}


def replay(summary):
    report, qrels, ranked = read_frozen(summary)
    derived_runs(report, ranked)
    if set(ranked) != set(report["arms"]):
        raise ValueError("missing frozen retrieval arm")
    data = {"queries": {qid: {} for qid in qrels}, "qrels": qrels,
            "manifest": report["dataset"], "hashes": report["input_hashes"]}
    for name, run in ranked.items():
        actual = evaluate(data, run, report["frozen_run_storage"]["metric_cutoffs"])
        expected = report["arms"][name]
        if actual["per_query"].keys() != expected["per_query"].keys():
            raise ValueError("missing frozen query")
        for qid, metrics in expected["per_query"].items():
            scalar_metrics(actual["per_query"][qid], metrics, name + "/" + qid)
        for group, metrics in expected["summary"].items():
            for metric, value in metrics.items():
                scalar_metrics(actual["summary"][group][metric], value, name + "/" + metric)
        for group, metrics in report["groups"][name].items():
            qids = report["group_query_ids"][group]
            if len(qids) != report["group_queries"][group]:
                raise ValueError("changed frozen group population")
            values = {metric: sum(actual["per_query"][q][metric] for q in qids) / len(qids)
                      for metric in metrics}
            scalar_metrics(values, metrics, name + "/" + group)
    return {"queries": len(qrels), "arms": len(ranked), "model_calls": 0,
            "database_calls": 0, "verified": "per-query metrics, summaries and fixed source groups",
            "scope": "Frozen ranks/labels only; does not rerun ingestion or model inference."}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("summary", type=Path)
    args = parser.parse_args()
    print(json.dumps(replay(args.summary), ensure_ascii=False))


if __name__ == "__main__":
    main()
