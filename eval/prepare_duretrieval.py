"""Prepare a pinned, explicitly sampled DuRetrieval comparison using existing BEIR tooling."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import tempfile
from pathlib import Path

from eval.prepare_t2_subset import select
from eval.rag_dataset import load

SOURCE = "https://huggingface.co/datasets/mteb/DuRetrieval"
REVISION = "8d9b3d6a6a62b7fd2d4df0821d612366dc52d14e"
FILES = {
    "corpus/dev-00000-of-00001.parquet":
        "dc60d5f9b5a642371c19602c94e79b4a6df2e1a667b0642d0980f4e47f072df2",
    "data/dev-00000-of-00001.parquet":
        "e310a25c8cfe7164fc4bed6e115dc9b43b02a8399c44e61ca28cadda93ae2ad4",
    "queries/dev-00000-of-00001.parquet":
        "bfb7b9433ab3ccbb1f18d1ded6f6ce8f3988f2bf65f374735714a26ba09089ea",
}


def prepare(source: Path, output: Path, *, seed: str, queries: int,
            background: int, pool: int) -> dict:
    if output.exists():
        raise FileExistsError("choose a new output directory")
    if min(queries, background, pool) < 1 or background > pool:
        raise ValueError("positive sizes required; background cannot exceed pool")
    for name, digest in FILES.items():
        with (source / name).open("rb") as stream:
            if hashlib.file_digest(stream, "sha256").hexdigest() != digest:
                raise ValueError("changed pinned DuRetrieval source: " + name)
    data = select(source, seed, queries, background, pool)
    manifest = {
        "schema_version": 1, "name": "DuRetrieval fixed Chinese comparison subset",
        "source": SOURCE, "revision": REVISION,
        "license": "Not declared in the pinned MTEB card; local evaluation only, "
                   "corpus is not redistributed by this project",
        "license_source": f"{SOURCE}/blob/{REVISION}/README.md",
        "scope": "Source dev subset, not an official full-corpus benchmark or blind holdout. "
                 "No document-length filtering or truncation. Unjudged is not negative.",
        "source_hashes": FILES,
        "selection": {"seed": seed, "queries": queries, "background": background,
                      "lexical_pool": pool, "unjudged_lexical_distractors_per_query": 1},
        "hard_candidates": data["hard"], "background_ids": data["background"],
        "limitations": "All original judgments for selected queries are retained. "
                       "Corpus reduction changes difficulty; lexical distractor selection "
                       "is conditioned on BM25. No independent no-answer labels. "
                       "Possible embedding training exposure to this public dataset.",
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=output.parent, prefix=".du-") as temporary:
        stage = Path(temporary) / "dataset"
        (stage / "qrels").mkdir(parents=True)
        for name in ("corpus", "queries"):
            with (stage / f"{name}.jsonl").open("w", encoding="utf-8") as stream:
                for key in sorted(data[name]):
                    stream.write(json.dumps(data[name][key], ensure_ascii=False) + "\n")
        with (stage / "qrels/dev.tsv").open("w", encoding="utf-8") as stream:
            stream.write("query-id\tcorpus-id\tscore\n")
            for qid in sorted(data["qrels"]):
                for did, grade in sorted(data["qrels"][qid].items()):
                    stream.write(f"{qid}\t{did}\t{grade}\n")
        (stage / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2)
                                             + "\n", encoding="utf-8")
        checked = load(stage, "dev")
        shutil.move(stage, output)
    return {"documents": len(checked["corpus"]), "queries": len(checked["queries"]),
            "input_hashes": checked["hashes"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--seed", default="saudade-du-20261112-v1")
    parser.add_argument("--queries", type=int, default=200)
    parser.add_argument("--background", type=int, default=8192)
    parser.add_argument("--pool", type=int, default=10000)
    args = parser.parse_args()
    print(json.dumps(prepare(args.source_dir, args.output, seed=args.seed,
                             queries=args.queries, background=args.background, pool=args.pool)))


if __name__ == "__main__":
    main()
