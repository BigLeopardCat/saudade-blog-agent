"""Prepare an explicitly sampled Chinese T2Ranking development regression set."""

from __future__ import annotations

import argparse
import hashlib
import heapq
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

import httpx

os.environ["SAUDADE_IGNORE_ENV_FILE"] = "1"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from eval.offline_rag_eval import bm25_run  # noqa: E402
from eval.rag_dataset import load  # noqa: E402

REVISION = "921dd3af6e78d1ae7ee0368aa8d7eaee02c8f08e"
SOURCE = "https://huggingface.co/datasets/mteb/T2Retrieval"
FILES = {
    "corpus/dev-00000-of-00001.parquet": (
        "8f0400530c6ba664e580e2ae7bb5ed41a908b8abc17c3b3934f42cc65992ac1e"
    ),
    "data/dev-00000-of-00001.parquet": (
        "c4c6ffd715faf151e0832a26dfcf2d7ec1edc17d9620a9aa99e721cc2436d7d2"
    ),
    "queries/dev-00000-of-00001.parquet": (
        "186b4d0a13cc4d8339eccfc6d465d1b2d4d1f6572defa8d39f3c8da3b66b0210"
    ),
}


def rows(path: Path):
    import pyarrow.parquet as parquet

    for batch in parquet.ParquetFile(path).iter_batches(batch_size=512, use_threads=False):
        yield from batch.to_pylist()


def priority(seed: str, kind: str, key: str) -> int:
    return int(hashlib.sha256(f"{seed}:{kind}:{key}".encode()).hexdigest(), 16)


def acquire(root: Path, download: bool) -> None:
    for name, digest in FILES.items():
        path = root / name
        if not path.is_file() and download:
            path.parent.mkdir(parents=True, exist_ok=True)
            with httpx.stream(
                "GET", f"{SOURCE}/resolve/{REVISION}/{name}", follow_redirects=True, timeout=90
            ) as response:
                response.raise_for_status()
                with path.with_suffix(".partial").open("wb") as stream:
                    size = 0
                    for block in response.iter_bytes():
                        size += len(block)
                        if size > 200_000_000:
                            raise ValueError("source file exceeds limit")
                        stream.write(block)
            path.with_suffix(".partial").rename(path)
        if not path.is_file():
            raise ValueError("missing pinned T2 source: " + name)
        with path.open("rb") as stream:
            if hashlib.file_digest(stream, "sha256").hexdigest() != digest:
                raise ValueError("changed pinned T2 source: " + name)


def eligible_queries(judgments: dict, root: Path, max_chars: int) -> list[str]:
    eligible = [q for q, scores in judgments.items() if any(v > 0 for v in scores.values())]
    if max_chars:
        lengths = {
            r["_id"]: len(r["text"]) for r in rows(root / "corpus/dev-00000-of-00001.parquet")
        }
        eligible = [q for q in eligible if all(lengths[d] <= max_chars for d in judgments[q])]
    return eligible


def select(
    root: Path,
    seed: str,
    query_count: int,
    background_count: int,
    pool_size: int,
    max_chars: int = 0,
) -> dict:
    judgments: dict[str, dict[str, int]] = {}
    for row in rows(root / "data/dev-00000-of-00001.parquet"):
        judgments.setdefault(row["query-id"], {})[row["corpus-id"]] = row["score"]
    eligible = eligible_queries(judgments, root, max_chars)
    if len(eligible) < query_count:
        raise ValueError("not enough judged queries")
    selected = sorted(eligible, key=lambda q: priority(seed, "query", q))[:query_count]
    qrels = {q: judgments[q] for q in selected}
    queries = {
        r["_id"]: r for r in rows(root / "queries/dev-00000-of-00001.parquet") if r["_id"] in qrels
    }
    required = {d for scores in qrels.values() for d in scores}
    corpus, reservoir = {}, []
    for row in rows(root / "corpus/dev-00000-of-00001.parquet"):
        did = row["_id"]
        if did in required:
            corpus[did] = row
        if max_chars and len(row["text"]) > max_chars:
            continue
        candidate = (-priority(seed, "document", did), did, row)
        if len(reservoir) < pool_size:
            heapq.heappush(reservoir, candidate)
        elif candidate[0] > reservoir[0][0]:
            heapq.heapreplace(reservoir, candidate)
    pool = {d: row for _, d, row in reservoir}
    if required - corpus.keys():
        raise ValueError("source is missing judged documents")
    probe = {"corpus": {**pool, **corpus}, "queries": queries}
    run, _ = bm25_run(probe, top_k=50, cliff=False)
    hard = {}
    for q, hits in run.items():
        candidates = [d for d in hits if d not in qrels[q]]
        # Retain queries with no lexical overlap; dropping them would bias the sample.
        hard[q] = candidates[0] if candidates else None
    background = sorted(pool.keys() - required, key=lambda d: priority(seed, "document", d))
    if len(background) < background_count:
        raise ValueError("not enough background documents")
    keep = (
        required | {d for d in hard.values() if d is not None} | set(background[:background_count])
    )
    all_rows = {**pool, **corpus}
    return {
        "corpus": {d: all_rows[d] for d in sorted(keep)},
        "queries": queries,
        "qrels": qrels,
        "hard": hard,
        "background": background[:background_count],
    }


def prepare(
    source: Path,
    output: Path,
    seed: str,
    queries: int,
    background: int,
    pool: int,
    max_chars: int = 0,
) -> dict:
    if output.exists():
        raise FileExistsError("choose a new output directory")
    data = select(source, seed, queries, background, pool, max_chars)
    manifest = {
        "schema_version": 1,
        "name": "T2Retrieval fixed Chinese development subset",
        "source": SOURCE,
        "revision": REVISION,
        "license": "Apache-2.0 as declared by the MTEB dataset card; no corpus redistribution",
        "license_source": f"{SOURCE}/blob/{REVISION}/README.md",
        "scope": "fixed dev subset; NOT official full benchmark; not a production quality gate",
        "selection": {
            "seed": seed,
            "queries": queries,
            "background": background,
            "lexical_pool": pool,
            "unjudged_lexical_distractors_per_query": 1,
        },
        "source_hashes": FILES,
        "hard_candidates": data["hard"],
        "background_ids": data["background"],
        "limitations": "Unjudged != negative; no required multi-evidence/no-answer labels; "
        "small sample and possible pretraining exposure; general web, not blog domain",
    }
    manifest["selection"]["max_document_chars"] = max_chars or None
    if max_chars:
        manifest["scope"] += f"; short-passage stratum <= {max_chars} characters only"
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=output.parent, prefix=".t2-") as temporary:
        stage = Path(temporary) / "dataset"
        (stage / "qrels").mkdir(parents=True)
        for name in ("corpus", "queries"):
            with (stage / f"{name}.jsonl").open("w", encoding="utf-8") as stream:
                for row in data[name].values():
                    stream.write(json.dumps(row, ensure_ascii=False) + "\n")
        with (stage / "qrels/dev.tsv").open("w", encoding="utf-8") as stream:
            stream.write("query-id\tcorpus-id\tscore\n")
            for q, scores in data["qrels"].items():
                for d, score in scores.items():
                    stream.write(f"{q}\t{d}\t{score}\n")
        (stage / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n"
        )
        checked = load(stage, "dev")
        shutil.move(stage, output)
    return {
        "documents": len(checked["corpus"]),
        "queries": len(checked["queries"]),
        "input_hashes": checked["hashes"],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-dir", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--download", action="store_true")
    parser.add_argument("--seed", default="saudade-t2-20261023-v1")
    parser.add_argument("--queries", default=24, type=int)
    parser.add_argument("--background", default=64, type=int)
    parser.add_argument("--pool", default=4096, type=int)
    parser.add_argument("--max-document-chars", default=0, type=int)
    args = parser.parse_args()
    if (
        min(args.queries, args.background, args.pool) < 1
        or args.background > args.pool
        or args.max_document_chars < 0
    ):
        parser.error("positive sizes required; background cannot exceed pool")
    acquire(args.source_dir, args.download)
    result = prepare(
        args.source_dir,
        args.output,
        args.seed,
        args.queries,
        args.background,
        args.pool,
        args.max_document_chars,
    )
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
