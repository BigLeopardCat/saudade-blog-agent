"""Evaluate fixed-pool scores, bounded source reads and the predeclared project decision."""

from __future__ import annotations

import argparse
import gzip
import json
import lzma
import math
import os
from pathlib import Path

os.environ["SAUDADE_IGNORE_ENV_FILE"] = "1"

import numpy as np  # noqa: E402

from eval.context_retrieval_eval import read_contexts  # noqa: E402
from eval.full_corpus_analysis import fusion  # noqa: E402
from eval.offline_rag_eval import evaluate  # noqa: E402
from eval.rag_dataset import load  # noqa: E402
from eval.replay_retrieval_ranks import read_frozen  # noqa: E402
from eval.reranker_inputs import sha256  # noqa: E402
from eval.reranker_run import cached_rows, input_digest  # noqa: E402
from eval.statistical_metrics import paired_delta, paired_outcomes  # noqa: E402
from rag.embedding_inputs import load_profile  # noqa: E402


def quantiles(values):
    if not values or not all(math.isfinite(v) and v >= 0 for v in values):
        raise ValueError("nonnegative finite recorded costs required")
    return {"mean": float(np.mean(values)), "p50": float(np.quantile(values, .5)),
            "p95": float(np.quantile(values, .95)), "p99": float(np.quantile(values, .99)),
            "max": float(max(values)), "sum": float(sum(values))}


def query_orders(candidates, scores, lexical, dense):
    ids = [row["document_id"] for row in candidates]
    expected = fusion(lexical, dense, 24)
    if ids != expected or ids != [row["document_id"] for row in scores]:
        raise ValueError("fixed candidate identities/order changed")
    values = {row["document_id"]: row["logit"] for row in scores}
    if not all(math.isfinite(v) for v in values.values()):
        raise ValueError("nonfinite frozen score")
    positions = {did: i for i, did in enumerate(ids)}
    vector_ranks = {did: i for i, did in enumerate(dense)}
    vector = sorted(ids, key=lambda d: (vector_ranks.get(d, len(dense)), positions[d]))
    reranked = sorted(ids, key=lambda d: (-values[d], positions[d]))
    if vector[:8] != dense[:8]:
        raise ValueError("same-pool vector top8 differs from original baseline")
    return {"vector": vector, "rrf": ids, "reranker": reranked}


def source_views(inputs, scores, ranked):
    views = {name: {"run": {}, "locations": {}} for name in ("vector", "rrf", "reranker")}
    audits, performance, routes = {}, {}, {}
    with gzip.open(inputs, "rt") as stream:
        for line in stream:
            row = json.loads(line)
            qid = row["query_id"]
            if qid in audits or qid not in scores:
                raise ValueError("duplicate input or missing score query")
            saved = scores[qid]
            if saved["input_sha256"] != input_digest(row):
                raise ValueError("frozen score/input hash mismatch")
            routes[qid] = {"lexical": list(ranked["bm25"][qid])[:24],
                           "vector": list(ranked["vector_reference"][qid])}
            orders = query_orders(row["candidates"], saved["scores"],
                                  routes[qid]["lexical"], routes[qid]["vector"])
            by_id = {p["document_id"]: p for p in row["candidates"]}
            for name, ids in orders.items():
                views[name]["run"][qid] = {did: -float(i + 1) for i, did in enumerate(ids)}
                views[name]["locations"][qid] = [{"document_id": did,
                    "matches": by_id[did]["retrieval_matches"]} for did in ids[:8]]
            audits[qid] = [{k: v for k, v in pair.items()
                            if k not in {"input_ids", "retrieval_matches"}}
                           for pair in row["candidates"]]
            performance[qid] = {k: v for k, v in saved.items()
                                if k not in {"query_id", "scores", "input_sha256"}}
    if audits.keys() != scores.keys() or audits.keys() != ranked["bm25"].keys():
        raise ValueError("missing/foreign evaluation query")
    return views, audits, performance, routes


def stages(data, views, audits):
    result = {}
    for qid, labels in data["qrels"].items():
        positives = {did for did, grade in labels.items() if grade > 0}
        ids = list(views["rrf"]["run"][qid])
        found = positives & set(ids)
        hits = {n: positives & set(list(a["run"][qid])[:8]) for n, a in views.items()}
        result[qid] = {"judged_positives": len(positives), "pool_positives": len(found),
            "pool_recall": len(found) / len(positives),
            "oracle8_recall_bound": min(8, len(found)) / len(positives),
            "missing_from_pool": sorted(positives - set(ids)),
            "rrf_suppressed_positives": sorted(found - hits["rrf"]),
            "reranker_suppressed_positives": sorted(found - hits["reranker"]),
            "rrf_miss_recovered": bool(found) and not hits["rrf"] and bool(hits["reranker"]),
            "vector_hit_reranker_miss": bool(hits["vector"]) and not bool(hits["reranker"]),
            "truncated_positive_candidates": [p["document_id"] for p in audits[qid]
                                               if p["truncated"] and p["document_id"] in positives]}
    return result


def score_views(data, views, profile, groups):
    documents, contexts, cohorts = {}, {}, {}
    for name, arm in views.items():
        documents[name] = evaluate(data, arm["run"], [1, 8, 24, 100])
        contexts[name] = read_contexts(data, arm, profile, mode="window")
        cohorts[name] = {group: {m: sum(documents[name]["per_query"][q][m] for q in ids)
            / len(ids) for m in ("R@8", "nDCG@8", "Success@8")}
            for group, ids in groups.items()}
        print("scored documents and budgeted source reads: " + name, flush=True)
    paired = {}
    for base in ("vector", "rrf"):
        paired[base] = {}
        for metric in ("R@8", "nDCG@8", "Success@8"):
            before = [documents[base]["per_query"][q][metric] for q in data["queries"]]
            after = [documents["reranker"]["per_query"][q][metric] for q in data["queries"]]
            paired[base][metric] = paired_delta(before, after)
            if metric == "Success@8":
                paired[base][metric]["outcomes"] = paired_outcomes([bool(v) for v in before],
                                                                         [bool(v) for v in after])
    return documents, contexts, cohorts, paired


def decision(documents, cohorts, performance, plan):
    def value(name, metric):
        return documents[name]["summary"]["all"][metric]["value"]

    budget = plan["decision_budget"]
    long_group = "has_positive_source_over_1800_chars"
    checks = {"ndcg_gain_over_both": min(value("reranker", "nDCG@8") - value(n, "nDCG@8")
        for n in ("vector", "rrf")) >= budget["ndcg8_gain_over_each_baseline_min"],
        "recall_vs_vector": value("reranker", "R@8") - value("vector", "R@8")
        >= budget["recall8_delta_vs_vector_min"],
        "long_source_recall_vs_vector": cohorts["reranker"][long_group]["R@8"]
        - cohorts["vector"][long_group]["R@8"] >= budget["long_source_recall8_delta_vs_vector_min"],
        "added_warm_p95": performance["seconds"]["p95"]
        <= budget["added_pretokenized_warm_query_p95_seconds_max"]}
    return {"checks": checks, "project_budget_passed": all(checks.values()),
            "business_validation_available": False, "production_enabled": False,
            "scope": "Predeclared public-dev/resource decision only; business validation absent."}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("dataset", "baseline", "inputs", "scores", "plan", "profile", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--known-source-audit", type=Path)
    args = parser.parse_args()
    data = load(args.dataset, "dev", max_docs=100001)
    frozen, _, ranked = read_frozen(args.baseline)
    metadata = json.loads(args.inputs.with_suffix(".metadata.json").read_text())
    config = json.loads(args.scores.with_suffix(".config.json").read_text())
    plan = json.loads(args.plan.read_text())
    if (frozen["input_hashes"] != data["hashes"] or metadata["input_hashes"] != data["hashes"]
            or metadata["output_sha256"] != sha256(args.inputs)
            or config["input_sha256"] != sha256(args.inputs)
            or config["model_sha256"] != plan["model"]["export_sha256"]):
        raise ValueError("model/input/baseline source binding changed")
    saved_scores = cached_rows(args.scores)
    views, audits, timings, routes = source_views(args.inputs, saved_scores, ranked)
    groups = frozen["group_query_ids"]
    documents, contexts, cohorts, paired = score_views(data, views, load_profile(str(args.profile)),
                                                      groups)
    performance = {name: quantiles([row[name] for row in timings.values()]) for name in
                   ("seconds", "cpu_seconds", "model_batches", "padded_tokens", "unpadded_tokens")}
    result = {"schema_version": 1, "dataset": data["manifest"], "input_hashes": data["hashes"],
        "evaluation_labels": {"queries": {q: {} for q in data["queries"]}, "qrels": data["qrels"]},
        "documents": documents, "contexts": contexts, "fixed_group_query_ids": groups,
        "groups": cohorts, "paired_comparisons": paired, "input_audits": audits,
        "query_performance": timings, "performance": performance, "plan": plan,
        "candidate_routes": routes,
        "candidate_scores": {q: row["scores"] for q, row in saved_scores.items()},
        "failure_stages": stages(data, views, audits), "model_runtime_binding": config,
        "source_artifacts": {name: sha256(getattr(args, name))
                             for name in ("baseline", "inputs", "scores", "plan")},
        "scope": "Fixed public-dev pool, community INT8 single cross-encoder; deterministic "
                 "read-contract replay, no answers/HTTP/semantic Judge. "
                 "Latency excludes tokenization/retrieval/queue; "
                 "frame costs are framework estimates.",
        "semantic_judge_calls": 0, "embedding_calls": 0, "cloud_model_calls": 0}
    if args.known_source_audit:
        with lzma.open(args.known_source_audit, "rt", encoding="utf-8") as stream:
            result["known_source_input_audit"] = json.load(stream)
        result["source_artifacts"]["known_source_input_audit"] = sha256(args.known_source_audit)
    result["decision"] = decision(documents, cohorts, performance, plan)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with lzma.open(args.output, "wt", encoding="utf-8") as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2)
    print(json.dumps(result["decision"]), flush=True)


if __name__ == "__main__":
    main()
