"""Run production vector retrieval and RRF over fixed BEIR data via a selected endpoint."""

import argparse
import hashlib
import json
import logging
import os
import sys
import time
from pathlib import Path

os.environ["SAUDADE_IGNORE_ENV_FILE"] = "1"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import settings  # noqa: E402
from eval.offline_rag_eval import evaluate  # noqa: E402
from eval.rag_dataset import load  # noqa: E402
from rag import chunks as chunking  # noqa: E402
from rag import vector_index as vectors
from rag.corpus import content_revision  # noqa: E402
from rag.embedding_inputs import active_profile, vector_chunks  # noqa: E402
from rag.search import RagIndex  # noqa: E402


def execute(data: dict, index_dir: Path, candidates: int, expected_dim: int) -> dict:
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
    # A common character window is only a fair control if neither model refines it further.
    if len(prepared) != len(index._chunks):
        raise ValueError("common chunks exceed selected profile; lower --chunk-chars for all arms")
    store = vectors.VectorStore(index_dir)
    vectors._store = store
    start = time.perf_counter()
    update = store.update(prepared, vectors.space_from_settings())
    if not update.get("ok", update.get("skipped") == "aligned") or store.missing():
        raise ValueError("incomplete vector index; no quality report will be published")
    if store.view() is None or store.view().dim != expected_dim:
        raise ValueError("unexpected actual vector dimension")
    build_seconds = time.perf_counter() - start
    runs: dict[str, dict] = {name: {} for name in ("bm25", "vector", "hybrid")}
    query_seconds = {}
    for qid, query in data["queries"].items():
        lexical = index._lexical_ranked(query["text"], candidates, apply_cliff=True)
        start = time.perf_counter()
        dense, reason, missing = index._vector_ranked(query["text"], candidates)
        query_seconds[qid] = time.perf_counter() - start
        if reason not in (None, "no_vector_hits") or missing or lexical is None:
            raise ValueError("query retrieval failed; do not silently score a degraded model")
        fused = vectors.rrf_fuse(lexical, dense, settings.rrf_k, candidates)
        for name, hits in (("bm25", lexical), ("vector", dense), ("hybrid", fused)):
            runs[name][qid] = {
                keys[h["id"] - 1]: float(len(hits) - rank) for rank, h in enumerate(hits)
            }
        print("completed query " + str(len(query_seconds)), flush=True)
    policy = active_profile()
    return {
        "dataset": data["manifest"],
        "input_hashes": data["hashes"],
        "model": settings.embedding_model,
        "profile": policy.spec.model_dump() if policy else None,
        "space": vectors.space_from_settings().as_dict(),
        "actual_dimension": expected_dim,
        "documents": len(keys),
        "queries": len(data["queries"]),
        "chunks": len(prepared),
        "config": {
            "common_chunk_chars": chunking.MAX_CHARS,
            "candidates": candidates,
            "rrf_k": settings.rrf_k,
            "batch_size": settings.embedding_batch_size,
        },
        "index_update": update,
        "build_seconds": build_seconds,
        "query_seconds": query_seconds,
        "latency_scope": (
            "warm model, cold query cache, single sequential client; no hardware isolation"
        ),
        "arms": {name: evaluate(data, run, [1, 3, 10]) for name, run in runs.items()},
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dataset", type=Path)
    parser.add_argument("--split", default="dev")
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--profile", default="")
    parser.add_argument("--expected-dim", type=int, required=True)
    parser.add_argument("--index-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--chunk-chars", type=int, default=384)
    parser.add_argument("--candidates", type=int, default=20)
    args = parser.parse_args()
    if not 64 <= args.chunk_chars <= 1800 or args.expected_dim < 1 or args.candidates < 10:
        parser.error("chunk chars 64..1800, positive dimension and candidates >=10 required")
    settings.embedding_model = args.model
    settings.embedding_base_url = args.base_url
    settings.embedding_api_key = os.getenv("EMBEDDING_API_KEY") or "local-only"
    settings.embedding_profile = args.profile
    settings.embedding_timeout = 120
    settings.embedding_batch_size = 1
    settings.rag_hybrid_enabled = True
    settings.rag_vector_dir = str(args.index_dir)
    chunking.MAX_CHARS = args.chunk_chars
    logging.basicConfig(level=logging.WARNING)
    log = logging.getLogger("rag.vector_index")
    log.setLevel(logging.DEBUG)
    root = Path(__file__).resolve().parents[1]
    sources = [
        root / "rag" / name
        for name in (
            "chunks.py",
            "corpus.py",
            "embedding_inputs.py",
            "ranking.py",
            "search.py",
            "sections.py",
            "vector_index.py",
            "vector_files.py",
        )
    ]
    sources.append(Path(__file__))
    code_hash = hashlib.sha256(b"".join(p.read_bytes() for p in sources)).hexdigest()
    data = load(args.dataset, args.split)
    report = execute(data, args.index_dir, args.candidates, args.expected_dim)
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
