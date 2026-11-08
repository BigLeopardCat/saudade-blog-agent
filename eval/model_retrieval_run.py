"""Run production vector retrieval and RRF over fixed BEIR data via a selected endpoint."""

import argparse
import hashlib
import json
import logging
import os
import sys
import time
from importlib.metadata import version
from pathlib import Path

os.environ["SAUDADE_IGNORE_ENV_FILE"] = "1"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import settings  # noqa: E402
from eval.offline_rag_eval import evaluate  # noqa: E402
from eval.rag_dataset import load  # noqa: E402
from rag import DEFAULT_TOP_K  # noqa: E402
from rag import chunks as chunking  # noqa: E402
from rag import vector_index as vectors
from rag.corpus import content_revision  # noqa: E402
from rag.embedding_inputs import active_profile, vector_chunks  # noqa: E402
from rag.lexical import backend_info  # noqa: E402
from rag.postgres import PostgresRepository  # noqa: E402
from rag.search import (  # noqa: E402
    BM25_B,
    BM25_K1,
    RagIndex,
    candidate_limit,
    expansion_enabled,
    synonyms,
)
from rag.vector_index import VectorStore  # noqa: E402


def execute(data: dict, collection: str, candidates: int, expected_dim: int, *,
            top_k: int = DEFAULT_TOP_K, cutoffs: list[int] | None = None) -> dict:
    cutoffs = sorted(set([k for k in (1, 3, DEFAULT_TOP_K) if k <= top_k]
                         if cutoffs is None else cutoffs))
    if not cutoffs or min(cutoffs) < 1 or max(cutoffs) > top_k or candidates < top_k:
        raise ValueError("positive cutoffs <= top-k <= candidates required")
    keys = sorted(data["corpus"])
    documents = [
        {
            "type": "note",
            "id": i,
            "title": data["corpus"][d].get("title", ""),
            "content": data["corpus"][d]["text"],
            "revision": content_revision(
                data["corpus"][d].get("title", ""), data["corpus"][d]["text"]
            ),
        }
        for i, d in enumerate(keys, 1)
    ]
    index = RagIndex(corpus_loader=lambda: documents, vector_warmer=lambda _: None)
    index.build()
    prepared = vector_chunks(index._chunks)
    vectors.close()
    store = VectorStore(PostgresRepository(settings.rag_vector_dsn, str(collection)))
    vectors._store = store
    start = time.perf_counter()
    update = store.update(prepared, vectors.space_from_settings())
    if not update.get("ok", update.get("skipped") == "aligned") or store.missing():
        raise ValueError("incomplete vector index; no quality report will be published")
    if store.view() is None or store.view().dim != expected_dim:
        raise ValueError("unexpected actual vector dimension")
    build_seconds = time.perf_counter() - start
    runs: dict[str, dict] = {name: {} for name in ("bm25", "vector", "hybrid")}
    query_seconds, phase_seconds = {}, {}
    with vectors._query_lock:
        cache_entries = len(vectors._query_cache)
    for qid, query in data["queries"].items():
        start = time.perf_counter()
        lexical = index._lexical_ranked(query["text"], candidates)
        lexical_done = time.perf_counter()
        dense, reason, missing = index._vector_ranked(query["text"], candidates)
        vector_done = time.perf_counter()
        if reason not in (None, "no_vector_hits") or missing or lexical is None:
            raise ValueError("query retrieval failed; do not silently score a degraded model")
        fused = vectors.rrf_fuse(lexical, dense, settings.rrf_k, top_k)
        end = time.perf_counter()
        query_seconds[qid] = end - start
        phase_seconds[qid] = {"lexical": lexical_done - start,
                              "vector": vector_done - lexical_done,
                              "fusion": end - vector_done}
        for name, hits in (("bm25", lexical), ("vector", dense), ("hybrid", fused)):
            runs[name][qid] = {
                keys[h["id"] - 1]: float(len(hits) - rank)
                for rank, h in enumerate(hits[:top_k])
            }
        print("completed query " + str(len(query_seconds)), flush=True)
    policy = active_profile()
    config = {
        "chunk_chars": chunking.MAX_CHARS, "chunk_overlap": chunking.OVERLAP,
        "chunking_signature": chunking.strategy_signature(),
        "candidates": candidates, "top_k": top_k, "cutoffs": cutoffs,
        "rrf_k": settings.rrf_k, "fusion_policy": vectors.FUSION_POLICY,
        "bm25_backend": backend_info(),
        "bm25_k1": BM25_K1, "bm25_b": BM25_B, "synonyms": synonyms(),
        "query_expansion": expansion_enabled(),
        "batch_size": settings.embedding_batch_size,
        "space": vectors.space_from_settings().as_dict(),
        "vector_backend": "pgvector-exact-document-cosine",
        "pgvector_python_version": version("pgvector"),
    }
    return {
        "schema_version": 2,
        "dataset": data["manifest"],
        "input_hashes": data["hashes"],
        "model": settings.embedding_model,
        "profile": policy.spec.model_dump() if policy else None,
        "space": vectors.space_from_settings().as_dict(),
        "actual_dimension": expected_dim,
        "documents": len(keys),
        "queries": len(data["queries"]),
        "chunks": len(prepared), "lexical_chunks": len(index._chunks),
        "config": config,
        "config_sha256": hashlib.sha256(json.dumps(
            config, sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
        "ir_measures_version": version("ir-measures"),
        "index_update": update,
        "build_seconds": build_seconds,
        "query_seconds": query_seconds,
        "phase_seconds": phase_seconds,
        "query_cache_initial_entries": cache_entries,
        "latency_scope": (
            "single sequential client; query_seconds includes lexical, vector and fusion; "
            "query cache may warm on repeated text; no hardware isolation"
        ),
        "arms": {name: evaluate(data, run, cutoffs) for name, run in runs.items()},
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dataset", type=Path)
    parser.add_argument("--split", default="dev")
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--profile", default="")
    parser.add_argument("--expected-dim", type=int, required=True)
    parser.add_argument("--collection", required=True, help="isolated eval- collection")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--chunk-chars", type=int, help="explicit research override")
    parser.add_argument("--top-k", type=int, default=DEFAULT_TOP_K)
    parser.add_argument("--candidates", type=int,
                        help="per-retriever candidate pool; defaults to the production policy")
    parser.add_argument("--k", type=int, nargs="+", help="metric cutoffs, independent of pool size")
    args = parser.parse_args(argv)
    candidates = args.candidates if args.candidates is not None else candidate_limit(args.top_k)
    cutoffs = args.k or [k for k in (1, 3, DEFAULT_TOP_K) if k <= args.top_k]
    if (args.chunk_chars is not None and args.chunk_chars < 64
            or args.expected_dim < 1 or args.top_k < 1 or candidates < args.top_k
            or not cutoffs or min(cutoffs) < 1 or max(cutoffs) > args.top_k):
        parser.error("chunk chars >=64, positive dimension and cutoffs <= top-k <= candidates")
    settings.embedding_model = args.model
    settings.embedding_base_url = args.base_url
    settings.embedding_api_key = os.getenv("EMBEDDING_API_KEY") or "local-only"
    settings.embedding_profile = args.profile
    settings.rag_hybrid_enabled = True
    settings.rag_vector_dsn = os.getenv("SAUDADE_RAG_EVAL_DSN", "")
    if not settings.rag_vector_dsn or not args.collection.startswith("eval-"):
        parser.error("explicit SAUDADE_RAG_EVAL_DSN and an eval- collection are required")
    settings.rag_vector_collection = args.collection
    production_chunk_chars = chunking.MAX_CHARS
    if args.chunk_chars is not None:
        chunking.MAX_CHARS = args.chunk_chars
    logging.basicConfig(level=logging.WARNING)
    log = logging.getLogger("rag.vector_index")
    log.setLevel(logging.DEBUG)
    root = Path(__file__).resolve().parents[1]
    sources = [*sorted((root / "rag").glob("*.py")), Path(__file__),
               root / "eval/offline_rag_eval.py", root / "eval/rag_dataset.py"]
    code_hash = hashlib.sha256(b"".join(p.read_bytes() for p in sources)).hexdigest()
    data = load(args.dataset, args.split)
    report = execute(data, args.collection, candidates, args.expected_dim,
                     top_k=args.top_k, cutoffs=cutoffs)
    report["split"] = args.split
    report["configuration_mode"] = ("research_override" if candidates != candidate_limit(args.top_k)
        or args.top_k != DEFAULT_TOP_K or chunking.MAX_CHARS != production_chunk_chars
        else "production_retrieval_defaults")
    report["code_sha256"] = code_hash
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n")
    run_directory = args.output.parent / "runs" / args.output.stem
    run_directory.mkdir(parents=True, exist_ok=True)
    for name, arm in report["arms"].items():
        with (run_directory / (name + ".run")).open("w") as stream:
            for qid, hits in arm["run"].items():
                for rank, (did, score) in enumerate(hits.items(), 1):
                    stream.write(f"{qid} Q0 {did} {rank} {score} {name}\n")
    print(json.dumps({name: arm["summary"]["all"] for name, arm in report["arms"].items()}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
