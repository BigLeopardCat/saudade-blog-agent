"""Recompute published reranking metrics/decision without models, token IDs or source bodies."""

from __future__ import annotations

import argparse
import json
import lzma
from pathlib import Path

from eval.replay_chunk_context import costs, metrics
from eval.replay_retrieval_ranks import scalar_metrics
from eval.reranker_analysis import decision, quantiles, query_orders, stages


def replay_input_audit(report):
    data = {**report["evaluation_labels"], "manifest": report["dataset"],
            "hashes": report["input_hashes"]}
    for name in ("original", "reranker_input"):
        arm = report[name]
        metrics(data, arm, [1, 3, 8], name, arm["locations"])
    pairs = report["input_audits"]
    if (len(pairs) != report["queries"]
            or sum(p["truncated"] for p in pairs.values()) != report["truncated_pairs"]):
        raise ValueError("known-source input population changed")
    for qid, pair in pairs.items():
        shown = report["reranker_input"]["locations"][qid]
        if shown[0]["matches"] != pair["visible_source"] or pair["input_tokens"] > 512:
            raise ValueError("known-source input visibility changed")
    return len(pairs)


def replay(report):
    data = {**report["evaluation_labels"], "manifest": report["dataset"],
            "hashes": report["input_hashes"]}
    documents = report["documents"]
    for name, arm in documents.items():
        actual = metrics(data, arm, [1, 8, 24, 100], name)
        for group, expected in report["groups"][name].items():
            ids = report["fixed_group_query_ids"][group]
            means = {m: sum(actual["per_query"][q][m] for q in ids) / len(ids) for m in expected}
            scalar_metrics(means, expected, name + "/" + group)
    for name, arm in report["contexts"].items():
        metrics(data, arm, [1, 3, 8], name + "/context", arm["locations"])
        costs(arm, name + "/context")
    for qid, pairs in report["input_audits"].items():
        route = report["candidate_routes"][qid]
        orders = query_orders(pairs, report["candidate_scores"][qid],
                              route["lexical"], route["vector"])
        for name, ids in orders.items():
            actual = sorted(documents[name]["run"][qid],
                            key=lambda d: -documents[name]["run"][qid][d])
            if actual != ids:
                raise ValueError("frozen score/rank mismatch")
        recorded = report["query_performance"][qid]
        batch = report["model_runtime_binding"]["batch_size"]
        padded = sum(max(p["input_tokens"] for p in pairs[i:i + batch])
                     * len(pairs[i:i + batch]) for i in range(0, len(pairs), batch))
        scalar_metrics(recorded, {"padded_tokens": padded,
            "unpadded_tokens": sum(p["input_tokens"] for p in pairs),
            "model_batches": (len(pairs) + batch - 1) // batch}, "observed model batches")
        for pair in pairs:
            if (not 0 < pair["input_tokens"] <= 512 or not pair["query_tokens"]
                    or pair["truncated"] != (pair["full_pair_tokens"] > pair["input_tokens"])):
                raise ValueError("frozen pair budget audit mismatch")
    for name, expected in report["performance"].items():
        actual = quantiles([row[name] for row in report["query_performance"].values()])
        scalar_metrics(actual, expected, "performance/" + name)
    if decision(documents, report["groups"], report["performance"], report["plan"]) \
            != report["decision"]:
        raise ValueError("frozen project decision changed")
    views = {name: {"run": arm["run"]} for name, arm in documents.items()}
    if stages(data, views, report["input_audits"]) != report["failure_stages"]:
        raise ValueError("frozen failure attribution changed")
    known_source = (replay_input_audit(report["known_source_input_audit"])
                    if "known_source_input_audit" in report else 0)
    return {"queries": len(data["queries"]), "known_source_audit_queries": known_source,
        "candidate_pairs": sum(
        len(p) for p in report["input_audits"].values()), "arms": len(documents),
        "model_calls": 0, "database_calls": 0, "verified": "fixed pools, score order, metrics, "
        "visible-read costs, model batches, recorded latency summaries and decision",
        "scope": "Does not rerun model inference, source parsing or hardware timing."}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path)
    args = parser.parse_args()
    with lzma.open(args.report, "rt", encoding="utf-8") as stream:
        report = json.load(stream)
    print(json.dumps(replay(report), ensure_ascii=False))


if __name__ == "__main__":
    main()
