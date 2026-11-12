"""Full-corpus BM25S reference and application aggregation, with identical features."""

from __future__ import annotations

import argparse
import gc
import hashlib
import json
import os
import time
from array import array
from pathlib import Path

os.environ["SAUDADE_IGNORE_ENV_FILE"] = "1"

import bm25s  # noqa: E402
import numpy as np  # noqa: E402
from bm25s.tokenization import Tokenized  # noqa: E402

from eval.rag_dataset import load  # noqa: E402
from rag.lexical import DTYPE, METHOD, LexicalIndex, backend_info  # noqa: E402
from rag.ranking import documents as rank_documents  # noqa: E402
from rag.search import BM25_B, BM25_K1, RagIndex, chunk_note, expansion_enabled  # noqa: E402
from rag.tokenization import query_features, tokenize  # noqa: E402
from rag.vector_index import chunk_text  # noqa: E402


def document_ranks(scores, owners, count, limit):
    """Reference max pooling; preserve corpus order when scores tie."""
    maximum = np.zeros(count, dtype=np.float64)
    np.maximum.at(maximum, owners, scores)
    order = np.lexsort((np.arange(count), -maximum))
    return [int(i) for i in order[:limit] if maximum[i] > 0]


def prepare_tokens(path, mode):
    vocab, token_ids, chunks, keys, owners = {}, [], [], [], []
    with path.open() as stream:
        for number, line in enumerate(stream):
            row = json.loads(line)
            keys.append(row["_id"])
            parts = (chunk_note(row.get("title", ""), row["text"]) if mode == "chunk"
                     else [{"text": row["text"], "section": "", "section_id": "s1"}])
            for part in parts:
                chunk = {**part, "type": "note", "id": number + 1,
                         "title": row.get("title", "")}
                terms = tokenize(chunk_text(chunk["title"], chunk["text"]))
                # BM25S's public Tokenized API avoids retaining one Python string
                # per token occurrence; feature values and frequencies are unchanged.
                token_ids.append(array("i", [vocab.setdefault(t, len(vocab)) for t in terms]))
                chunks.append(chunk)
                owners.append(number)
            if len(keys) % 5000 == 0:
                print(f"tokenized {len(keys)} documents", flush=True)
    return Tokenized(ids=token_ids, vocab=vocab), chunks, keys, np.asarray(owners)


def execute(dataset, mode, output, limit):
    started = time.perf_counter()
    data = load(dataset, "dev", max_docs=100001)
    hashes = data["hashes"]
    # Ground-truth evaluation needs IDs, not another retained copy of every body.
    del data["corpus"]
    tokens, chunks, keys, owners = prepare_tokens(dataset / "corpus.jsonl", mode)
    model = bm25s.BM25(k1=BM25_K1, b=BM25_B, method=METHOD, dtype=DTYPE,
                       backend="numpy", csc_backend="numpy")
    model.index(tokens, show_progress=False)
    del tokens
    gc.collect()
    adapter = object.__new__(LexicalIndex)
    adapter.count, adapter.model = len(chunks), model
    adapter.vocabulary = frozenset(model.vocab_dict)
    index = RagIndex(corpus_loader=lambda: [], vector_warmer=lambda _: None)
    index._chunks, index._lexical, index._revision = chunks, adapter, hashes["corpus.jsonl"]
    build_seconds = time.perf_counter() - started
    metadata = {"schema_version": 1, "mode": mode, "documents": len(keys),
                "chunks": len(chunks), "top_k": limit, "input_hashes": hashes,
                "backend": backend_info(), "build_seconds": build_seconds,
                "scope": "BM25S public token-ID build; identical application features. "
                         "Application query scoring/aggregation compared to native scores. "
                         "No whole RagIndex refresh or full string-token constructor benchmark.",
                "reference_query_expansion": False,
                "application_query_expansion": expansion_enabled(), "semantic_judge_calls": 0}
    output.parent.mkdir(parents=True, exist_ok=True)
    output.with_suffix(".metadata.json").write_text(json.dumps(metadata, indent=2) + "\n")
    with output.open("w") as stream:
        for number, (qid, query) in enumerate(data["queries"].items(), 1):
            start = time.perf_counter()
            terms = list(dict.fromkeys(t for t in query_features(query["text"])
                                      if t in adapter.vocabulary))
            scores = model.get_scores(terms) if terms else np.zeros(len(chunks))
            reference = document_ranks(scores, owners, len(keys), limit)
            direct_done = time.perf_counter()
            actual = adapter.scores(terms)
            hits = rank_documents(chunks, actual, hashes["corpus.jsonl"], limit=limit)
            raw_application = [row["id"] - 1 for row in hits]
            assert raw_application == reference, f"BM25 adapter/max aggregation mismatch: {qid}"
            # This offline run holds one frozen corpus; wall-clock refresh is not part
            # of the reference comparison and must not refetch an unrelated corpus.
            index._last_build = time.time()
            production = [row["id"] - 1 for row in index._lexical_ranked(query["text"], limit)]
            positive_chunks = np.flatnonzero(scores > 0)
            order = positive_chunks[np.argsort(-scores[positive_chunks], kind="stable")]
            chunk_first = list(dict.fromkeys(int(owners[i]) for i in order[:limit]))
            record = {"query_id": qid, "reference": [keys[i] for i in reference],
                      "production": [keys[i] for i in production],
                      "raw_application": [keys[i] for i in raw_application],
                      "chunk_first": [keys[i] for i in chunk_first],
                      "seconds": {"reference": direct_done - start,
                                  "application": time.perf_counter() - direct_done}}
            stream.write(json.dumps(record, ensure_ascii=False) + "\n")
            stream.flush()
            if number % 100 == 0:
                print(f"scored {number} queries", flush=True)
    metadata["output_sha256"] = hashlib.sha256(output.read_bytes()).hexdigest()
    output.with_suffix(".metadata.json").write_text(json.dumps(metadata, indent=2) + "\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dataset", type=Path)
    parser.add_argument("--mode", choices=["document", "chunk"], required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--top-k", type=int, default=100)
    args = parser.parse_args()
    if not 1 <= args.top_k <= 1000:
        parser.error("bounded top-k required")
    execute(args.dataset, args.mode, args.output, args.top_k)


if __name__ == "__main__":
    main()
