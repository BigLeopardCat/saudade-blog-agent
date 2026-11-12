"""Compare PostgreSQL document retrieval with scikit-learn exact cosine on identical vectors."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import time
from pathlib import Path

os.environ["SAUDADE_IGNORE_ENV_FILE"] = "1"

import numpy as np  # noqa: E402
from sqlalchemy import select  # noqa: E402

from eval.full_corpus_cache import inventory_path, inventory_rows, same_encoder  # noqa: E402
from eval.full_corpus_lexical import document_ranks  # noqa: E402
from eval.rag_dataset import load  # noqa: E402
from eval.retrieval_evidence import locations  # noqa: E402
from rag.embedding_inputs import active_profile, load_profile, query_text  # noqa: E402
from rag.postgres import PostgresRepository, embeddings  # noqa: E402
from rag.ranking import documents as rank_documents  # noqa: E402
from rag.vector_index import SEP, Space, _embed_texts, space_from_settings  # noqa: E402


def export_matrix(repository, positions, count, path=None):
    matrix = (np.lib.format.open_memmap(path, mode="w+", dtype="float32", shape=(count, 512))
              if path is not None else np.empty((count, 512), dtype="float32"))
    exported = set()
    with repository.engine.connect() as connection:
        result = connection.execution_options(stream_results=True).execute(select(
            embeddings.c.key, embeddings.c.embedding).where(
                embeddings.c.collection == repository.collection))
        for key, vector in result:
            if key in positions:
                matrix[positions[key]] = vector
                exported.add(key)
    if exported != positions.keys():
        raise ValueError("vector export did not cover inventory")
    if isinstance(matrix, np.memmap):
        matrix.flush()
    return matrix


def publish_inventory(repository, inventory, root, *, in_memory=False):
    chunks, keys, positions, document_ids, parts = [], [], {}, [], []
    for row in inventory_rows(inventory):
        chunk = row["chunk"]
        positions.setdefault(row["key"], []).append(len(keys))
        keys.append(row["key"])
        document_ids.append(row["document_id"])
        chunks.append({"type": chunk["type"], "id": chunk["id"]})
        parts.append(f"{chunk['type']}{SEP}{chunk['id']}{SEP}{chunk['section']}{SEP}{row['key']}")
    fingerprint = hashlib.sha256("\n".join(parts).encode()).hexdigest()
    space = space_from_settings().as_dict()
    namespace = hashlib.sha256(json.dumps(space, sort_keys=True).encode()).hexdigest()
    available = repository.available(namespace, 512)
    if not positions.keys() <= available.keys():
        raise ValueError("incomplete cache; no full-corpus scoring")
    repository.commit(rows=[], chunks=chunks, keys=keys, space=space, space_key=namespace,
                      fingerprint=fingerprint, dimension=512, stats={"reused": len(positions)},
                      complete=True)
    matrix = export_matrix(repository, positions, len(keys),
                           None if in_memory else root / "vectors.npy")
    doc_keys = sorted(set(document_ids))
    lookup = {key: i for i, key in enumerate(doc_keys)}
    return matrix, doc_keys, np.asarray([lookup[key] for key in document_ids])


def checked_query_cache(root, expected, dimension):
    path = root / "queries.npy"
    saved = json.loads((root / "queries.metadata.json").read_text())
    if ({key: saved.get(key) for key in expected} != expected or
            saved.get("vectors_sha256") != hashlib.sha256(path.read_bytes()).hexdigest()):
        raise ValueError("query vector binding changed")
    vectors = np.load(path)
    if (vectors.shape != (len(expected["query_ids"]), dimension)
            or not np.isfinite(vectors).all() or not (np.linalg.norm(vectors, axis=1) > 0).all()):
        raise ValueError("query vector dimension, finiteness or norm changed")
    return vectors, saved


def reuse_query_vectors(data, source, destination, old_profile, dimension=512):
    """Rebind frozen queries only when document splitting is the sole encoder-policy difference."""
    previous, current = load_profile(str(old_profile)), active_profile()
    same_encoder(previous, current)
    space = space_from_settings()
    old_space = Space(space.model, space.base_url, space.dim, previous.signature, space.chunking)
    expected = {"input_hashes": data["hashes"], "query_ids": list(data["queries"]),
                "space": old_space.as_dict()}
    _, saved = checked_query_cache(source, expected, dimension)
    if (destination / "queries.npy").exists():
        raise ValueError("do not overwrite an existing frozen query cache")
    destination.mkdir(parents=True, exist_ok=True)
    start = time.perf_counter()
    shutil.copyfile(source / "queries.npy", destination / "queries.npy")
    saved.update(space=space.as_dict(), reused_query_encoder={
        "source_space": old_space.as_dict(), "source_vectors_sha256": saved["vectors_sha256"],
        "new_embedding_calls": 0, "copy_seconds": time.perf_counter() - start,
        "scope": "Identical query encoder/input; only document splitting changed. "
                 "materialization_seconds retains original source cold cost."})
    (destination / "queries.metadata.json").write_text(json.dumps(saved, indent=2) + "\n")


def query_vectors(data, root, expected_dim=512):
    started = time.perf_counter()
    path, metadata_path = root / "queries.npy", root / "queries.metadata.json"
    qids = list(data["queries"])
    if any(not data["queries"][qid]["text"].strip() or
           len(data["queries"][qid]["text"].strip()) > 32768 for qid in qids):
        raise ValueError("query exceeds application input limits")
    metadata = {"input_hashes": data["hashes"], "query_ids": qids,
                "space": space_from_settings().as_dict()}
    if path.exists():
        values, _ = checked_query_cache(root, metadata, expected_dim)
        return qids, values
    profile = active_profile()
    inputs = [query_text(data["queries"][qid]["text"].strip(), profile) for qid in qids]
    unique = list(dict.fromkeys(inputs))
    encoded = _embed_texts(unique, space_from_settings())
    if len(encoded) != len(unique):
        raise ValueError("incomplete query embeddings")
    cached = dict(zip(unique, encoded, strict=True))
    values = [cached[value] for value in inputs]
    if any(value is None or len(value) != expected_dim for value in values):
        raise ValueError("incomplete query embeddings")
    vectors = np.asarray(values, dtype="float32")
    norms = np.linalg.norm(vectors, axis=1)
    if not np.isfinite(norms).all() or not (norms > 0).all():
        raise ValueError("nonfinite or zero query embeddings")
    np.save(path, vectors)
    metadata["vectors_sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    metadata["unique_embedding_inputs"] = len(unique)
    metadata["materialization_seconds"] = time.perf_counter() - started
    metadata_path.write_text(json.dumps(metadata, indent=2) + "\n")
    return qids, vectors


def reference_locations(scores, owners, ranked, keys, chunks, source_keys, top_k):
    indices = np.flatnonzero(np.isin(owners, ranked[:top_k]))
    hits = rank_documents(chunks, {int(i): float(scores[i]) for i in indices if scores[i] > 0},
                          "frozen-source", retriever="vector", limit=top_k,
                          include_excerpts=False)
    result = locations(hits, source_keys)
    if [row["document_id"] for row in result] != [keys[i] for i in ranked[:top_k]]:
        raise ValueError("document aggregation locations differ from reference ranks")
    return result


def compare_queries(repository, matrix, owners, keys, qids, queries, output, limit,
                    verify_queries=None, *, chunks=None, source_keys=None, location_top_k=0):
    from sklearn.metrics.pairwise import cosine_similarity

    snapshot = repository.snapshot()
    verified = set(np.linspace(0, len(qids) - 1, min(len(qids), verify_queries), dtype=int)
                   if verify_queries is not None else range(len(qids)))
    metadata = {"schema_version": 1, "queries": len(qids), "documents": len(keys),
                "chunks": len(matrix), "top_k": limit, "score_tolerance": 2e-6,
                "scope": "Same Q8 embedding inputs/vectors; scikit-learn exact cosine "
                         "versus production PostgreSQL cosine/max document aggregation. "
                         "Does not test Q8 versus original model weights or answer quality.",
                "semantic_judge_calls": 0}
    metadata.update(postgres_verification_queries=len(verified),
                    production_complete=len(verified) == len(qids),
                    quality_backend="scikit-learn exact cosine on exported production vectors",
                    location_documents=location_top_k)
    with output.open("w") as stream:
        for start in range(0, len(qids), 16):
            began = time.perf_counter()
            similarities = cosine_similarity(queries[start:start + 16], matrix)
            reference_seconds = time.perf_counter() - began
            for offset, scores in enumerate(similarities):
                number = start + offset
                reference = document_ranks(scores, owners, len(keys), limit)
                order = np.argsort(-scores, kind="stable")[:limit]
                chunk_first = list(dict.fromkeys(int(owners[i]) for i in order if scores[i] > 0))
                locators = ({"reference_locations": reference_locations(
                    scores, owners, reference, keys, chunks, source_keys, location_top_k)}
                    if location_top_k else {})
                if number not in verified:
                    record = {"query_id": qids[number], "reference": [keys[i] for i in reference],
                        "production": None, "postgres_verified": False,
                        "chunk_first": [keys[i] for i in chunk_first],
                        "max_score_error": None, "near_tie_rank_difference": None,
                        "seconds": {"reference_batch_amortized": reference_seconds
                                    / len(similarities), "postgres": None}, **locators}
                    stream.write(json.dumps(record) + "\n")
                    continue
                began = time.perf_counter()
                selected = repository.scores(snapshot, queries[number].tolist(), limit)
                postgres_seconds = time.perf_counter() - began
                sql_scores = np.zeros(len(matrix), dtype="float64")
                for index, score in selected.items():
                    sql_scores[index] = score
                actual = document_ranks(sql_scores, owners, len(keys), limit)
                maximum = np.zeros(len(keys), dtype="float64")
                np.maximum.at(maximum, owners, scores)
                floor = min((maximum[i] for i in actual), default=0)
                missing = [i for i in reference if i not in actual and maximum[i] > floor + 2e-6]
                error = max((abs(scores[i] - score) for i, score in selected.items()), default=0)
                if missing or error > 2e-6:
                    raise ValueError(f"PostgreSQL/reference mismatch: {qids[number]}, {error}")
                record = {"query_id": qids[number], "reference": [keys[i] for i in reference],
                          "production": [keys[i] for i in actual],
                          "postgres_verified": True,
                          "chunk_first": [keys[i] for i in chunk_first],
                          "max_score_error": float(error),
                          "near_tie_rank_difference": actual != reference,
                          "seconds": {"reference_batch_amortized": (
                              reference_seconds / len(similarities)),
                                      "postgres": postgres_seconds}, **locators}
                stream.write(json.dumps(record) + "\n")
                stream.flush()
            print(f"scored {min(start + 16, len(qids))} dense queries", flush=True)
    return metadata


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dataset", type=Path)
    parser.add_argument("--artifacts", type=Path, required=True)
    parser.add_argument("--collection", required=True)
    parser.add_argument("--top-k", type=int, default=100)
    parser.add_argument("--postgres-verify-queries", type=int,
                        help="explicit evenly spaced SQL controls; unverified rows stay null")
    parser.add_argument("--locations-top-k", type=int, default=0,
                        help="source locators from the existing aggregator; omit display excerpts")
    parser.add_argument("--matrix-in-memory", action="store_true",
                        help="avoid a regenerable on-disk duplicate; vectors stay in PostgreSQL")
    args = parser.parse_args()
    dsn = os.environ.get("SAUDADE_RAG_EVAL_DSN", "")
    if not dsn or not args.collection.startswith("eval-") or not 1 <= args.top_k <= 1000:
        parser.error("explicit disposable DSN, eval- collection and bounded top-k required")
    if args.postgres_verify_queries is not None and args.postgres_verify_queries < 1:
        parser.error("positive PostgreSQL verification sample required")
    if not 0 <= args.locations_top_k <= args.top_k:
        parser.error("location document budget must fit the ranked candidate window")
    data = load(args.dataset, "dev", max_docs=100001)
    repository = PostgresRepository(dsn, args.collection)
    matrix, keys, owners = publish_inventory(repository, inventory_path(args.artifacts),
                                             args.artifacts, in_memory=args.matrix_in_memory)
    qids, queries = query_vectors(data, args.artifacts)
    chunks = ([row["chunk"] for row in inventory_rows(inventory_path(args.artifacts))]
              if args.locations_top_k else None)
    output = args.artifacts / "dense.jsonl"
    result = compare_queries(repository, matrix, owners, keys, qids, queries, output, args.top_k,
                             args.postgres_verify_queries, chunks=chunks,
                             source_keys=sorted(data["corpus"]),
                             location_top_k=args.locations_top_k)
    result.update(input_hashes=data["hashes"], space=space_from_settings().as_dict(),
                  matrix_bytes_sha256=hashlib.sha256(memoryview(matrix)).hexdigest(),
                  output_sha256=hashlib.sha256(output.read_bytes()).hexdigest())
    output.with_suffix(".metadata.json").write_text(json.dumps(result, indent=2) + "\n")
    repository.close()


if __name__ == "__main__":
    main()
