"""Score frozen full-corpus references, candidate budgets and document aggregation."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from collections import Counter
from pathlib import Path

os.environ["SAUDADE_IGNORE_ENV_FILE"] = "1"

from langchain_classic.retrievers.ensemble import EnsembleRetriever  # noqa: E402
from langchain_core.documents import Document  # noqa: E402
from langchain_core.runnables import RunnableLambda  # noqa: E402

from eval.full_corpus_cache import inventory_path, inventory_rows  # noqa: E402
from eval.offline_rag_eval import evaluate  # noqa: E402
from eval.prepare_t2_subset import priority  # noqa: E402
from eval.rag_dataset import load  # noqa: E402
from eval.statistical_metrics import paired_delta, paired_outcomes  # noqa: E402
from rag.vector_index import rrf_fuse  # noqa: E402


def read_stage(path, data):
    metadata = json.loads(path.with_suffix(".metadata.json").read_text())
    if metadata.get("production_complete") is False:
        raise ValueError("partial SQL controls are not a complete production ranking run")
    if (metadata["input_hashes"] != data["hashes"] or
            metadata["output_sha256"] != hashlib.sha256(path.read_bytes()).hexdigest()):
        raise ValueError("stage source or output hash mismatch")
    with path.open() as stream:
        rows = [json.loads(line) for line in stream]
    result = {row["query_id"]: row for row in rows}
    if len(result) != len(rows) or result.keys() != data["queries"].keys():
        raise ValueError("duplicate or incomplete stage queries")
    return result, metadata


def fusion(left, right, pool):
    inputs = [[Document(page_content=key, metadata={"id": key}) for key in rows[:pool]]
              for rows in (left, right)]
    ensemble = EnsembleRetriever(retrievers=[RunnableLambda(lambda _: []),
                                            RunnableLambda(lambda _: [])],
                                 weights=[.5, .5], c=60, id_key="id")
    reference = [d.metadata["id"] for d in ensemble.weighted_reciprocal_rank(inputs)]
    application = rrf_fuse(*[[{"type": "note", "id": d.metadata["id"]} for d in rows]
                             for rows in inputs], 60, 100)
    assert reference[:100] == [row["id"] for row in application]
    return reference[:100]


def runs(lexical, whole, dense, jieba):
    result = {name: {} for name in ["bm25", "bm25_raw", "bm25_whole_document",
        "vector", "vector_reference", "bm25_chunk_first100", "vector_chunk_first100",
        "hybrid24", "hybrid50", "hybrid100", "bm25_jieba_whole_document"]}
    failures = {}
    for qid, row in lexical.items():
        values = {"bm25": row["production"], "bm25_raw": row["reference"],
                  "bm25_whole_document": whole[qid]["reference"],
                  "bm25_jieba_whole_document": jieba[qid]["reference"],
                  "vector": dense[qid]["production"],
                  "vector_reference": dense[qid]["reference"],
                  "bm25_chunk_first100": row["chunk_first"],
                  "vector_chunk_first100": dense[qid]["chunk_first"]}
        for pool in (24, 50, 100):
            values[f"hybrid{pool}"] = fusion(values["bm25"], values["vector"], pool)
        for name, ids in values.items():
            result[name][qid] = {key: float(len(ids) - i) for i, key in enumerate(ids)}
        failures[qid] = values
    return result, failures


def source_groups(data, chunk_counts):
    order = sorted(data["queries"], key=lambda q: priority("saudade-du-20261112-v1", "query", q))
    groups = {"prior_200": order[:200], "additional_1800": order[200:]}
    groups["has_positive_source_over_1800_chars"] = [q for q in order if any(
        grade > 0 and len(data["corpus"][did]["text"]) > 1800
        for did, grade in data["qrels"][q].items())]
    groups["has_positive_source_with_multiple_vector_chunks"] = [q for q in order if any(
        grade > 0 and chunk_counts.get(did, 0) > 1
        for did, grade in data["qrels"][q].items())]
    return order, groups


def candidate_pools(data, rankings):
    """Label-based upper bounds describe available candidates, not a deployable ranker."""
    result = {}
    for pool in (24, 50, 100):
        samples = []
        for qid, values in rankings.items():
            positive = {did for did, grade in data["qrels"][qid].items() if grade > 0}
            if not positive:
                continue
            union = set(values["bm25"][:pool]) | set(values["vector"][:pool])
            found = positive & union
            final = positive & set(values[f"hybrid{pool}"][:8])
            samples.append({"union_positive_recall": len(found) / len(positive),
                "oracle_recall8_bound": min(8, len(found)) / len(positive),
                "union_has_positive": bool(found),
                "positive_in_pool_but_final8_miss": bool(found) and not bool(final),
                "vector8_hit_hybrid8_miss": bool(positive & set(values["vector"][:8]))
                                           and not bool(final)})
        result[str(pool)] = {"positive_queries": len(samples),
            "mean_union_positive_recall": sum(s["union_positive_recall"] for s in samples)
                                          / len(samples),
            "mean_oracle_recall8_bound": sum(s["oracle_recall8_bound"] for s in samples)
                                        / len(samples),
            **{key: sum(s[key] for s in samples) for key in (
                "union_has_positive", "positive_in_pool_but_final8_miss",
                "vector8_hit_hybrid8_miss")}}
    return result


def summarize(data, scored, rankings, chunk_counts):
    order, groups = source_groups(data, chunk_counts)
    buckets, differences, stages = {}, {}, {}
    for name, arm in scored.items():
        buckets[name] = {group: {metric: sum(arm["per_query"][q][metric] for q in qids) / len(qids)
            for metric in arm["per_query"][qids[0]]} for group, qids in groups.items() if qids}
    for before, after in [("bm25", "vector"), ("vector", "hybrid24"),
                          ("bm25_whole_document", "bm25_raw"), ("hybrid24", "hybrid100")]:
        comparisons = {}
        for metric in ("Success@8", "R@8", "nDCG@8", "R@100"):
            left = [scored[before]["per_query"][q][metric] for q in order]
            right = [scored[after]["per_query"][q][metric] for q in order]
            comparisons[metric] = paired_delta(left, right)
            if metric.startswith("Success"):
                comparisons[metric]["outcomes"] = paired_outcomes(
                    [bool(x) for x in left], [bool(x) for x in right])
        differences[f"{before}_to_{after}"] = comparisons
    for qid, values in rankings.items():
        positive = {key for key, grade in data["qrels"][qid].items() if grade > 0}
        union24 = set(values["bm25"][:24]) | set(values["vector"][:24])
        union100 = set(values["bm25"]) | set(values["vector"])
        final = set(values["hybrid24"][:8])
        stages[qid] = {"judged_positives": len(positive),
            "missing_from_union24": sorted(positive - union24),
            "missing_from_union100": sorted(positive - union100),
            "in_union24_but_not_final8": sorted((positive & union24) - final),
            "in_union100_but_not_final8": sorted((positive & union100) - final),
            "vector8_hit_hybrid8_miss": bool(positive & set(values["vector"][:8]))
                                      and not bool(positive & final)}
    return {"groups": buckets, "group_queries": {g: len(q) for g, q in groups.items()},
            "group_query_ids": groups,
            "candidate_pools": candidate_pools(data, rankings),
            "paired_comparisons": differences, "failure_stages": stages,
            "source_indexing": {"source_documents": len(data["corpus"]),
                "documents_with_vector_chunks": len(chunk_counts),
                "zero_vector_chunk_document_ids": sorted(
                    data["corpus"].keys() - chunk_counts.keys())}}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dataset", type=Path)
    parser.add_argument("--artifacts", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    data = load(args.dataset, "dev", max_docs=100001)
    lexical, lex_meta = read_stage(args.artifacts / "lexical-chunk.jsonl", data)
    whole, whole_meta = read_stage(args.artifacts / "lexical-document.jsonl", data)
    jieba, jieba_meta = read_stage(args.artifacts / "lexical-jieba.jsonl", data)
    dense, dense_meta = read_stage(args.artifacts / "dense.jsonl", data)
    inventory_metadata = json.loads((args.artifacts / "inventory.json").read_text())
    if inventory_metadata["input_hashes"] != data["hashes"]:
        raise ValueError("inventory source binding changed")
    chunk_counts = Counter(row["document_id"] for row in
                           inventory_rows(inventory_path(args.artifacts)))
    ranked, per_query = runs(lexical, whole, dense, jieba)
    scored = {name: evaluate(data, run, [1, 3, 8, 10, 24, 50, 100])
              for name, run in ranked.items()}
    result = {"schema_version": 1, "input_hashes": data["hashes"], "dataset": data["manifest"],
              "stages": {"lexical_chunk": lex_meta, "lexical_document": whole_meta,
                         "jieba_document": jieba_meta, "dense": dense_meta}, "arms": scored,
              **summarize(data, scored, per_query, chunk_counts), "semantic_judge_calls": 0,
              "scope": "Source dev full corpus. Fixed ablations, no parameter selection. "
                       "Shared pretrained model and document-level qrels: cannot validate "
                       "chapter evidence, generated answers or production success. "
                       "Query bootstrap is descriptive, not an independent production SLA."}
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({name: row["summary"]["all"] for name, row in scored.items()}))


if __name__ == "__main__":
    main()
