"""Evaluate existing retrieval or a standard TREC run using ir-measures."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import sys
import time
from importlib.metadata import version
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import ir_measures  # noqa: E402

from eval.rag_dataset import load  # noqa: E402


def bm25_run(data: dict, top_k: int, cliff: bool) -> tuple[dict, dict]:
    # Dedicated offline process: ignore deployment .env before importing settings.
    os.environ["SAUDADE_IGNORE_ENV_FILE"] = "1"
    from rag.corpus import content_revision
    from rag.search import RagIndex

    keys = sorted(data["corpus"])
    documents = []
    for number, did in enumerate(keys, 1):
        row = data["corpus"][did]
        title, text = row.get("title", ""), row["text"]
        documents.append(
            {
                "type": "note",
                "id": number,
                "title": title,
                "content": text,
                "revision": content_revision(title, text),
            }
        )
    index = RagIndex(corpus_loader=lambda: documents, vector_warmer=lambda _: None)
    start = time.perf_counter()
    index.build()
    timings = {"build_seconds": time.perf_counter() - start, "query_seconds": {}}
    run = {}
    for qid, query in data["queries"].items():
        start = time.perf_counter()
        hits = index._lexical_ranked(query["text"], top_k, apply_cliff=cliff)
        if hits is None:
            raise RuntimeError("offline index unavailable")
        timings["query_seconds"][qid] = time.perf_counter() - start
        # Strict ranks preserve production tie order through TREC metric providers.
        run[qid] = {keys[h["id"] - 1]: float(len(hits) - rank) for rank, h in enumerate(hits)}
    return run, timings


def read_run(path: Path, data: dict) -> dict:
    run: dict[str, dict[str, float]] = {qid: {} for qid in data["queries"]}
    for row in ir_measures.read_trec_run(str(path)):
        if row.query_id not in run or row.doc_id not in data["corpus"]:
            raise ValueError("run references unknown query/document")
        if row.doc_id in run[row.query_id] or not math.isfinite(row.score):
            raise ValueError("run has duplicate document or non-finite score")
        run[row.query_id][row.doc_id] = row.score
    return run


def evaluate(data: dict, run: dict, cutoffs: list[int]) -> dict:
    if set(run) != set(data["queries"]):
        raise ValueError("run must explicitly include all evaluated queries, including empty runs")
    ranked_run = {
        q: {
            d: float(len(hits) - i) for i, d in enumerate(sorted(hits, key=lambda d: (-hits[d], d)))
        }
        for q, hits in run.items()
    }
    positive = {q: r for q, r in data["qrels"].items() if any(v > 0 for v in r.values())}
    measures = [
        m @ k
        for k in cutoffs
        for m in (ir_measures.Success, ir_measures.R, ir_measures.RR, ir_measures.nDCG)
    ]
    per_query = {qid: {} for qid in run}
    if positive:
        scored = {q: ranked_run[q] for q in positive}
        for result in ir_measures.iter_calc(measures, positive, scored):
            per_query[result.query_id][str(result.measure)] = result.value
    for qid, hits in run.items():
        query = data["queries"][qid]
        groups = query.get("evidence_groups", [])
        ranked = sorted(hits, key=lambda d: (-hits[d], d))
        for k in cutoffs:
            if groups:
                found = set(ranked[:k])
                per_query[qid][f"EvidenceCoverage@{k}"] = sum(
                    bool(found.intersection(g)) for g in groups
                ) / len(groups)
        if qid not in positive:
            per_query[qid]["CandidateReturnedOnUnanswerable"] = float(bool(hits))
    buckets: dict[str, list[str]] = {"all": list(run)}
    for qid, query in data["queries"].items():
        buckets.setdefault("category:" + query.get("category", "unspecified"), []).append(qid)
    summary = {}
    for bucket, qids in buckets.items():
        names = {name for q in qids for name in per_query[q]}
        summary[bucket] = {
            name: {
                "value": sum(per_query[q][name] for q in qids if name in per_query[q])
                / sum(name in per_query[q] for q in qids),
                "queries": sum(name in per_query[q] for q in qids),
            }
            for name in sorted(names)
        }
    return {"summary": summary, "per_query": per_query, "run": run}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dataset", type=Path)
    parser.add_argument("--split", default="test")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--run", type=Path, help="external API/local-model run in TREC format")
    parser.add_argument("--run-info", type=Path, help="external run provenance JSON")
    parser.add_argument("--k", type=int, nargs="+", default=[1, 3, 10])
    parser.add_argument("--max-docs", type=int, default=10000)
    parser.add_argument("--no-cliff", action="store_true")
    args = parser.parse_args(argv)
    if not args.k or min(args.k) < 1 or args.max_docs < 1:
        parser.error("cutoffs and max-docs must be positive")
    if bool(args.run) != bool(args.run_info):
        parser.error("--run and --run-info must be supplied together")
    run_info = None
    if args.run_info:
        run_info = json.loads(args.run_info.read_text(encoding="utf-8"))
        if not isinstance(run_info, dict) or any(
            not run_info.get(k) for k in ("name", "revision", "config")
        ):
            parser.error("run-info requires name, revision and nonempty config")
    data = load(args.dataset, args.split, args.max_docs)
    timings = None
    if args.run:
        run = read_run(args.run, data)
    else:
        run, timings = bm25_run(data, max(args.k), not args.no_cliff)
    result = evaluate(data, run, sorted(set(args.k)))
    from rag.chunks import INDEX_VERSION
    from rag.search import BM25_B, BM25_K1, cliff_config, synonyms

    root = Path(__file__).resolve().parents[1]
    sources = [*sorted((root / "rag").glob("*.py")), Path(__file__), root / "eval/rag_dataset.py"]
    result.update(
        schema_version=1,
        run_info=run_info,
        tie_policy="score descending, document ID ascending",
        evaluated_queries=len(data["queries"]),
        total_documents=len(data["corpus"]),
        dataset=data["manifest"],
        input_hashes=data["hashes"],
        split=args.split,
        ir_measures_version=version("ir-measures"),
        engine="external-run" if args.run else "production-bm25",
        code_hash=hashlib.sha256(
            b"".join(p.name.encode() + b"\0" + p.read_bytes() + b"\0" for p in sources)
        ).hexdigest(),
        timings=timings,
        config={
            "cutoffs": args.k,
            "index_version": INDEX_VERSION,
            "bm25_k1": BM25_K1,
            "bm25_b": BM25_B,
            "cliff": None if args.no_cliff else cliff_config(),
            "synonyms": synonyms(),
        },
        run_hash=(hashlib.sha256(args.run.read_bytes()).hexdigest() if args.run else None),
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8"
    )
    print(json.dumps(result["summary"], ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
