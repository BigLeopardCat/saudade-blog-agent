"""Populate only a disposable evaluation cache, checkpointing bounded embedding batches.

This does not publish production membership or change VectorStore's transaction contract.
The normal repository publishes the complete inventory after every input is available.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import time
from pathlib import Path

os.environ["SAUDADE_IGNORE_ENV_FILE"] = "1"

from sqlalchemy import literal, select  # noqa: E402
from sqlalchemy.dialects.postgresql import insert  # noqa: E402

from config import settings  # noqa: E402
from eval.rag_dataset import load  # noqa: E402
from rag.embedding_inputs import active_profile, document_text, vector_chunks  # noqa: E402
from rag.postgres import PostgresRepository, embeddings  # noqa: E402
from rag.search import chunk_note  # noqa: E402
from rag.vector_index import (  # noqa: E402
    _embed_texts,
    _materialize,
    chunk_key,
    space_from_settings,
)


def inventory(data, path):
    profile, space = active_profile(), space_from_settings()
    keys = sorted(data["corpus"])
    with path.open("w", encoding="utf-8") as stream:
        for number, did in enumerate(keys, 1):
            row = data["corpus"][did]
            chunks = [{**chunk, "type": "note", "id": number, "title": row.get("title", "")}
                      for chunk in chunk_note(row.get("title", ""), row["text"])]
            for chunk in vector_chunks(chunks):
                value = document_text(chunk["title"], chunk["text"], profile)
                stream.write(json.dumps({"document_id": did, "chunk": chunk,
                    "key": chunk_key(space, value), "input": value}, ensure_ascii=False) + "\n")


def populate(repository, path, progress):
    space = space_from_settings()
    space_key = hashlib.sha256(json.dumps(space.as_dict(), sort_keys=True).encode()).hexdigest()
    available = repository.available(space_key, 512)
    initial = len(available)
    pending, seen, total, calls = [], set(), 0, 0
    started = time.monotonic()

    def flush():
        nonlocal calls, total
        values = _embed_texts([row["input"] for row in pending], space)
        embedded, failed = _materialize([(r["key"], r["input"]) for r in pending], values, 512, 512)
        if len(values) != len(pending) or failed:
            raise ValueError("incomplete embedding batch; no snapshot published")
        with repository.engine.begin() as connection:
            connection.execute(insert(embeddings).on_conflict_do_nothing(), [
                {"collection": repository.collection, "space": space_key,
                 "key": key, "dimension": 512, "embedding": list(value)}
                for key, value in embedded])
        total += len(pending)
        calls += 1
        state = {"new_inputs": total, "reused_available": initial, "committed_batches": calls,
                 "seconds": time.monotonic() - started, "complete": False,
                 "scope": "offline checkpoint cache; not a published corpus"}
        progress.with_suffix(".partial").write_text(json.dumps(state) + "\n")
        progress.with_suffix(".partial").replace(progress)
        print(json.dumps(state), flush=True)
        pending.clear()

    with path.open() as stream:
        for line in stream:
            row = json.loads(line)
            key = row["key"]
            if key in seen:
                continue
            seen.add(key)
            if key not in available:
                pending.append(row)
            if len(pending) >= settings.embedding_batch_size:
                flush()
    if pending:
        flush()
    assert seen <= repository.available(space_key, 512).keys()
    state = json.loads(progress.read_text()) if progress.exists() else {}
    state.update(complete=True, unique_inputs=len(seen), space=space.as_dict(),
                 inventory_sha256=hashlib.sha256(path.read_bytes()).hexdigest())
    progress.write_text(json.dumps(state, indent=2) + "\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dataset", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--collection", required=True)
    parser.add_argument("--reuse-collection")
    args = parser.parse_args()
    if not args.collection.startswith("eval-") or (args.reuse_collection and
            not args.reuse_collection.startswith("eval-")):
        parser.error("only explicit eval- collections are allowed")
    dsn = os.environ.get("SAUDADE_RAG_EVAL_DSN", "")
    if not dsn:
        parser.error("explicit disposable SAUDADE_RAG_EVAL_DSN required")
    if not settings.embedding_configured or not settings.embedding_base_url.strip():
        parser.error("explicit embedding endpoint, model and credentials required")
    repository = PostgresRepository(dsn, args.collection)
    args.output.mkdir(parents=True, exist_ok=True)
    data = load(args.dataset, "dev", max_docs=100001)
    path = args.output / "inventory.jsonl"
    metadata = {"input_hashes": data["hashes"], "space": space_from_settings().as_dict()}
    manifest = args.output / "inventory.json"
    if path.exists():
        if json.loads(manifest.read_text()) != metadata:
            raise ValueError("inventory input/space changed")
    else:
        temporary = path.with_suffix(".partial")
        inventory(data, temporary)
        temporary.replace(path)
        manifest.write_text(json.dumps(metadata, indent=2) + "\n")
    del data
    if args.reuse_collection:
        with repository.engine.begin() as connection:
            connection.execute(insert(embeddings).from_select(
                ["collection", "key", "dimension", "space", "embedding"],
                select(literal(args.collection), embeddings.c.key,
                       embeddings.c.dimension, embeddings.c.space, embeddings.c.embedding)
                .where(embeddings.c.collection == args.reuse_collection)
            ).on_conflict_do_nothing())
    populate(repository, path, args.output / "embedding-progress.json")
    repository.close()


if __name__ == "__main__":
    main()
