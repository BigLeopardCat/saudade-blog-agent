"""Populate only a disposable evaluation cache, checkpointing bounded embedding batches.

This does not publish production membership or change VectorStore's transaction contract.
The normal repository publishes the complete inventory after every input is available.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import shutil
import time
from pathlib import Path

os.environ["SAUDADE_IGNORE_ENV_FILE"] = "1"

from sqlalchemy import String, column, literal, select, values  # noqa: E402
from sqlalchemy.dialects.postgresql import insert  # noqa: E402

from config import settings  # noqa: E402
from eval.rag_dataset import load  # noqa: E402
from rag.embedding_inputs import (  # noqa: E402
    active_profile,
    document_text,
    load_profile,
    vector_chunks,
)
from rag.postgres import PostgresRepository, embeddings  # noqa: E402
from rag.search import chunk_note  # noqa: E402
from rag.vector_index import (  # noqa: E402
    Space,
    _embed_texts,
    _materialize,
    chunk_key,
    space_from_settings,
)


def inventory_path(root):
    files = [root / name for name in ("inventory.jsonl", "inventory.jsonl.gz")
             if (root / name).exists()]
    if len(files) != 1:
        raise ValueError("one explicit plain or gzip inventory required")
    return files[0]


def inventory_rows(path):
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8") as stream:
        yield from (json.loads(line) for line in stream)


def same_encoder(before, after):
    omitted = {"tokenizer_file", "chunking_strategy"}
    if before.spec.model_dump(exclude=omitted) != after.spec.model_dump(exclude=omitted):
        raise ValueError("embedding encoder changed; cannot reuse by request text")


def rekey_reuse(repository, source, old_inventory, old_profile, new_inventory):
    """Explicit eval-only reuse of identical inputs when only splitting changed."""
    if (not source.startswith("eval-") or not repository.collection.startswith("eval-")
            or source == repository.collection):
        raise ValueError("distinct eval collections required for cross-policy reuse")
    profile, space = active_profile(), space_from_settings()
    previous = load_profile(str(old_profile))
    same_encoder(previous, profile)
    old_space = Space(space.model, space.base_url, space.dim, previous.signature, space.chunking)
    old_meta = json.loads((old_inventory.parent / "inventory.json").read_text())
    new_meta = json.loads((new_inventory.parent / "inventory.json").read_text())
    if (old_meta["input_hashes"] != new_meta["input_hashes"]
            or old_meta["space"] != old_space.as_dict()):
        raise ValueError("reuse source corpus or encoder space changed")
    old_namespace = hashlib.sha256(
        json.dumps(old_space.as_dict(), sort_keys=True).encode()).hexdigest()
    with repository.engine.connect() as connection:
        keys = set(connection.scalars(select(embeddings.c.key).where(
            embeddings.c.collection == source, embeddings.c.space == old_namespace,
            embeddings.c.dimension == 512)))
    lookup = {}
    for row in inventory_rows(old_inventory):
        if row["key"] != chunk_key(old_space, row["input"]):
            raise ValueError("reuse inventory input/key mismatch")
        if row["key"] in keys:
            lookup[hashlib.sha256(row["input"].encode()).digest()] = row["key"]
    pairs = {}
    for row in inventory_rows(new_inventory):
        if row["key"] != chunk_key(space, row["input"]):
            raise ValueError("target inventory input/key mismatch")
        old_key = lookup.get(hashlib.sha256(row["input"].encode()).digest())
        if old_key:
            pairs[row["key"]] = old_key
    namespace = hashlib.sha256(json.dumps(space.as_dict(), sort_keys=True).encode()).hexdigest()
    items = list(pairs.items())
    with repository.engine.begin() as connection:
        for start in range(0, len(items), 1000):
            mapping = values(column("new_key", String), column("old_key", String)).data(
                items[start:start + 1000]).alias("mapping")
            statement = select(literal(repository.collection), mapping.c.new_key,
                embeddings.c.dimension, literal(namespace), embeddings.c.embedding).join_from(
                    mapping, embeddings, mapping.c.old_key == embeddings.c.key).where(
                        embeddings.c.collection == source, embeddings.c.space == old_namespace,
                        embeddings.c.dimension == 512)
            connection.execute(insert(embeddings).from_select(
                ["collection", "key", "dimension", "space", "embedding"], statement
            ).on_conflict_do_nothing())
    return len(pairs)


def inventory(data, path):
    profile, space = active_profile(), space_from_settings()
    keys = sorted(data["corpus"])
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "wt", encoding="utf-8") as stream:
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
        if shutil.disk_usage(progress.parent).free < 128 * 1024 * 1024:
            raise OSError("insufficient scratch disk before embedding; checkpoints retained")
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

    for row in inventory_rows(path):
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
    parser.add_argument("--reuse-inventory", type=Path)
    parser.add_argument("--reuse-profile", type=Path)
    parser.add_argument("--compress-inventory", action="store_true")
    args = parser.parse_args()
    if not args.collection.startswith("eval-") or (args.reuse_collection and
            not args.reuse_collection.startswith("eval-")):
        parser.error("only explicit eval- collections are allowed")
    if (bool(args.reuse_inventory) != bool(args.reuse_profile) or args.reuse_inventory
            and (not args.reuse_collection or args.reuse_collection == args.collection)):
        parser.error("rekey requires an inventory/profile pair and a distinct source collection")
    dsn = os.environ.get("SAUDADE_RAG_EVAL_DSN", "")
    if not dsn:
        parser.error("explicit disposable SAUDADE_RAG_EVAL_DSN required")
    if not settings.embedding_configured or not settings.embedding_base_url.strip():
        parser.error("explicit embedding endpoint, model and credentials required")
    repository = PostgresRepository(dsn, args.collection)
    args.output.mkdir(parents=True, exist_ok=True)
    data = load(args.dataset, "dev", max_docs=100001)
    path = args.output / ("inventory.jsonl.gz" if args.compress_inventory else "inventory.jsonl")
    metadata = {"input_hashes": data["hashes"], "space": space_from_settings().as_dict()}
    manifest = args.output / "inventory.json"
    if path.exists():
        if json.loads(manifest.read_text()) != metadata:
            raise ValueError("inventory input/space changed")
    else:
        temporary = path.with_name(".partial-" + path.name)
        inventory(data, temporary)
        temporary.replace(path)
        manifest.write_text(json.dumps(metadata, indent=2) + "\n")
    del data
    if args.reuse_inventory:
        reused = rekey_reuse(repository, args.reuse_collection, args.reuse_inventory,
                             args.reuse_profile, path)
        print(json.dumps({"exact_inputs_rekeyed": reused}), flush=True)
    elif args.reuse_collection:
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
