"""Compare PostgreSQL document retrieval with scikit-learn exact cosine on identical vectors."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import time
from pathlib import Path

os.environ["SAUDADE_IGNORE_ENV_FILE"] = "1"

import numpy as np  # noqa: E402
from sqlalchemy import select  # noqa: E402

from eval.full_corpus_lexical import document_ranks  # noqa: E402
from eval.rag_dataset import load  # noqa: E402
from rag.embedding_inputs import active_profile, query_text  # noqa: E402
from rag.postgres import PostgresRepository, embeddings  # noqa: E402
from rag.vector_index import SEP, _embed_texts, space_from_settings  # noqa: E402


def publish_inventory(repository, inventory, root):
    chunks, keys, positions, document_ids, parts = [], [], {}, [], []
    with inventory.open() as stream:
        for line in stream:
            row = json.loads(line)
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
    matrix = np.lib.format.open_memmap(root / "vectors.npy", mode="w+", dtype="float32",
                                      shape=(len(keys), 512))
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
    matrix.flush()
    doc_keys = sorted(set(document_ids))
    lookup = {key: i for i, key in enumerate(doc_keys)}
    return matrix, doc_keys, np.asarray([lookup[key] for key in document_ids])


def query_vectors(data, root):
    path, metadata_path = root / "queries.npy", root / "queries.metadata.json"
    qids = list(data["queries"])
    metadata = {"input_hashes": data["hashes"], "query_ids": qids,
                "space": space_from_settings().as_dict()}
    if path.exists():
        saved = json.loads(metadata_path.read_text())
        if ({key: saved.get(key) for key in metadata} != metadata or
                saved.get("vectors_sha256") != hashlib.sha256(path.read_bytes()).hexdigest()):
            raise ValueError("query vector binding changed")
        return qids, np.load(path)
    profile = active_profile()
    inputs = [query_text(data["queries"][qid]["text"].strip(), profile) for qid in qids]
    unique = list(dict.fromkeys(inputs))
    encoded = _embed_texts(unique, space_from_settings())
    if len(encoded) != len(unique):
        raise ValueError("incomplete query embeddings")
    cached = dict(zip(unique, encoded, strict=True))
    values = [cached[value] for value in inputs]
    if any(value is None or len(value) != 512 for value in values):
        raise ValueError("incomplete query embeddings")
    vectors = np.asarray(values, dtype="float32")
    norms = np.linalg.norm(vectors, axis=1)
    if not np.isfinite(norms).all() or not (norms > 0).all():
        raise ValueError("nonfinite or zero query embeddings")
    np.save(path, vectors)
    metadata["vectors_sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    metadata["unique_embedding_inputs"] = len(unique)
    metadata_path.write_text(json.dumps(metadata, indent=2) + "\n")
    return qids, vectors


def compare_queries(repository, matrix, owners, keys, qids, queries, output, limit):
    from sklearn.metrics.pairwise import cosine_similarity

    snapshot = repository.snapshot()
    metadata = {"schema_version": 1, "queries": len(qids), "documents": len(keys),
                "chunks": len(matrix), "top_k": limit, "score_tolerance": 2e-6,
                "scope": "Same Q8 embedding inputs/vectors; scikit-learn exact cosine "
                         "versus production PostgreSQL cosine/max document aggregation. "
                         "Does not test Q8 versus original model weights or answer quality.",
                "semantic_judge_calls": 0}
    with output.open("w") as stream:
        for start in range(0, len(qids), 16):
            began = time.perf_counter()
            similarities = cosine_similarity(queries[start:start + 16], matrix)
            reference_seconds = time.perf_counter() - began
            for offset, scores in enumerate(similarities):
                number = start + offset
                reference = document_ranks(scores, owners, len(keys), limit)
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
                order = np.argsort(-scores, kind="stable")[:limit]
                chunk_first = list(dict.fromkeys(int(owners[i]) for i in order if scores[i] > 0))
                record = {"query_id": qids[number], "reference": [keys[i] for i in reference],
                          "production": [keys[i] for i in actual],
                          "chunk_first": [keys[i] for i in chunk_first],
                          "max_score_error": float(error),
                          "near_tie_rank_difference": actual != reference,
                          "seconds": {"reference_batch_amortized": (
                              reference_seconds / len(similarities)),
                                      "postgres": postgres_seconds}}
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
    args = parser.parse_args()
    dsn = os.environ.get("SAUDADE_RAG_EVAL_DSN", "")
    if not dsn or not args.collection.startswith("eval-") or not 1 <= args.top_k <= 1000:
        parser.error("explicit disposable DSN, eval- collection and bounded top-k required")
    data = load(args.dataset, "dev", max_docs=100001)
    repository = PostgresRepository(dsn, args.collection)
    matrix, keys, owners = publish_inventory(repository, args.artifacts / "inventory.jsonl",
                                             args.artifacts)
    qids, queries = query_vectors(data, args.artifacts)
    output = args.artifacts / "dense.jsonl"
    result = compare_queries(repository, matrix, owners, keys, qids, queries, output, args.top_k)
    result.update(input_hashes=data["hashes"], space=space_from_settings().as_dict(),
                  output_sha256=hashlib.sha256(output.read_bytes()).hexdigest())
    output.with_suffix(".metadata.json").write_text(json.dumps(result, indent=2) + "\n")
    repository.close()


if __name__ == "__main__":
    main()
