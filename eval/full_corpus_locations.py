"""Recover source locators with existing ranking over fixed full-corpus library scores."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path

os.environ["SAUDADE_IGNORE_ENV_FILE"] = "1"

import bm25s  # noqa: E402
import numpy as np  # noqa: E402

from eval.full_corpus_cache import inventory_path, inventory_rows  # noqa: E402
from eval.full_corpus_dense import compare_queries, export_matrix, query_vectors  # noqa: E402
from eval.full_corpus_lexical import document_ranks, prepare_tokens  # noqa: E402
from eval.rag_dataset import load  # noqa: E402
from eval.replay_retrieval_ranks import read_frozen  # noqa: E402
from eval.retrieval_evidence import locations  # noqa: E402
from rag.lexical import DTYPE, METHOD  # noqa: E402
from rag.postgres import PostgresRepository  # noqa: E402
from rag.ranking import documents as rank_documents  # noqa: E402
from rag.search import BM25_B, BM25_K1  # noqa: E402
from rag.tokenization import query_features  # noqa: E402


def lexical(data, dataset, frozen, output):
    report, _, ranked = read_frozen(frozen)
    if report["input_hashes"] != data["hashes"]:
        raise ValueError("frozen lexical rank source changed")
    expected = {qid: list(hits)[:24] for qid, hits in ranked["bm25_raw"].items()}
    del ranked, report
    del data["corpus"]
    tokens, chunks, keys, owners = prepare_tokens(dataset / "corpus.jsonl", "chunk")
    model = bm25s.BM25(k1=BM25_K1, b=BM25_B, method=METHOD, dtype=DTYPE,
                       backend="numpy", csc_backend="numpy")
    model.index(tokens, show_progress=False)
    del tokens
    with output.open("w") as stream:
        for number, (qid, query) in enumerate(data["queries"].items(), 1):
            terms = list(dict.fromkeys(term for term in query_features(query["text"])
                                       if term in model.vocab_dict))
            scores = model.get_scores(terms) if terms else np.zeros(len(chunks))
            references = document_ranks(scores, owners, len(keys), 24)
            if [keys[i] for i in references] != expected[qid]:
                raise ValueError("lexical ranking changed while recovering source locators")
            indices = np.flatnonzero(np.isin(owners, references))
            hits = rank_documents(chunks, {int(i): float(scores[i]) for i in indices
                                          if scores[i] > 0},
                                  "frozen-source", limit=24, include_excerpts=False)
            row = {"query_id": qid, "reference_locations": locations(hits, keys)}
            stream.write(json.dumps(row) + "\n")
            if number % 100 == 0:
                print(f"located {number} lexical queries", flush=True)
    return {"queries": len(data["queries"]), "ranked_documents": 24,
            "rank_verification": "Every top-24 list equals the frozen full-corpus BM25S run",
            "frozen_summary_sha256": hashlib.sha256(frozen.read_bytes()).hexdigest(),
            "model_calls": 0, "postgres_score_calls": 0,
            "scope": "Native BM25S scores and production aggregation, display excerpts omitted."}


def vector(data, artifacts, output, collection):
    if not collection.startswith("eval-") or not os.getenv("SAUDADE_RAG_EVAL_DSN"):
        raise ValueError("explicit disposable DSN and eval collection required")
    rows = list(inventory_rows(inventory_path(artifacts)))
    keys = sorted(set(row["document_id"] for row in rows))
    lookup = {key: i for i, key in enumerate(keys)}
    owners = np.asarray([lookup[row["document_id"]] for row in rows])
    chunks = [row["chunk"] for row in rows]
    positions = {}
    for index, row in enumerate(rows):
        positions.setdefault(row["key"], []).append(index)
    del rows
    repository = PostgresRepository(os.environ["SAUDADE_RAG_EVAL_DSN"], collection)
    try:
        matrix = (np.load(artifacts / "vectors.npy", mmap_mode="r")
                  if (artifacts / "vectors.npy").exists()
                  else export_matrix(repository, positions, len(chunks)))
        if matrix.shape != (len(chunks), 512):
            raise ValueError("source inventory and vector export differ")
        qids, queries = query_vectors(data, artifacts)
        result = compare_queries(repository, matrix, owners, keys, qids, queries, output, 100,
                                 verify_queries=0, chunks=chunks,
                                 source_keys=sorted(data["corpus"]), location_top_k=24)
    finally:
        repository.close()
    result["scope"] = ("Frozen baseline vectors and exact-cosine source-location replay. "
                       "No new SQL score verification; baseline already has 2,000 full controls.")
    result["matrix_bytes_sha256"] = hashlib.sha256(memoryview(matrix)).hexdigest()
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dataset", type=Path)
    parser.add_argument("--mode", choices=["lexical", "vector"], required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--artifacts", type=Path)
    parser.add_argument("--frozen-summary", type=Path)
    parser.add_argument("--collection", default="")
    args = parser.parse_args()
    if ((args.mode == "lexical" and args.frozen_summary is None)
            or args.mode == "vector" and args.artifacts is None):
        parser.error("lexical needs frozen summary; vector needs frozen artifacts")
    data = load(args.dataset, "dev", max_docs=100001)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    result = (lexical(data, args.dataset, args.frozen_summary, args.output)
              if args.mode == "lexical" else vector(data, args.artifacts, args.output,
                                                    args.collection))
    result.update(input_hashes=data["hashes"],
                  output_sha256=hashlib.sha256(args.output.read_bytes()).hexdigest(),
                  semantic_judge_calls=0)
    args.output.with_suffix(".metadata.json").write_text(json.dumps(result, indent=2) + "\n")


if __name__ == "__main__":
    main()
