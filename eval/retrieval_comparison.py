"""Describe paired retrieval changes and observable failure stages; never run a judge."""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from pathlib import Path

from eval.latency_metrics import distribution
from eval.rag_dataset import load
from eval.statistical_metrics import paired_delta, paired_outcomes


def failure_stage(query: dict, judgments: dict, result: dict, cutoff: int) -> str:
    positive = {did for did, grade in judgments.items() if grade > 0}
    if not positive:
        return "unanswerable_candidate_observation"
    if result[f"Success@{cutoff}"] == 0:
        return "no_relevant_document_in_top_k"
    if result.get(f"EvidenceCoverage@{cutoff}", 1) < 1:
        return "incomplete_document_evidence"
    if query.get("evidence_spans"):
        value = result.get(f"PassageEvidenceCoverage@{cutoff}")
        if value is None:
            return "passage_locations_unverified"
        if value < 1:
            return "document_hit_but_passage_evidence_incomplete"
    return "retrieval_checks_passed"


def analyze(report: dict, data: dict, cutoff: int) -> dict:
    if report["input_hashes"] != data["hashes"]:
        raise ValueError("report and dataset hashes differ")
    arms = report["arms"]
    if set(arms) != {"bm25", "vector", "hybrid"}:
        raise ValueError("expected all three retrieval arms")
    if cutoff not in report["config"]["cutoffs"] or any(
            set(arm["per_query"]) != set(data["queries"]) for arm in arms.values()):
        raise ValueError("missing cutoff or evaluated query")
    metrics = [f"Success@{cutoff}", f"R@{cutoff}", f"RR@{cutoff}", f"nDCG@{cutoff}",
               f"EvidenceCoverage@{cutoff}", f"PassageEvidenceCoverage@{cutoff}",
               f"AllPassageEvidence@{cutoff}"]
    comparisons = {}
    for before, after in (("bm25", "vector"), ("bm25", "hybrid"), ("vector", "hybrid")):
        paired = {}
        for metric in metrics:
            ids = [qid for qid in data["queries"] if all(
                metric in arms[name]["per_query"][qid] for name in (before, after))]
            left, right = ([arms[name]["per_query"][qid][metric] for qid in ids]
                           for name in (before, after))
            if ids:
                paired[metric] = {**paired_delta(left, right),
                    "wins": sum(b > a for a, b in zip(left, right, strict=True)),
                    "losses": sum(b < a for a, b in zip(left, right, strict=True))}
                if metric.startswith(("Success@", "AllPassageEvidence@")):
                    paired[metric]["outcomes"] = paired_outcomes(
                        [bool(x) for x in left], [bool(x) for x in right])
        comparisons[f"{before}_to_{after}"] = paired
    failures = {}
    counts = {name: Counter() for name in arms}
    for qid, query in data["queries"].items():
        stages = {name: failure_stage(query, data["qrels"][qid], arm["per_query"][qid], cutoff)
                  for name, arm in arms.items()}
        for name, stage in stages.items():
            counts[name][stage] += 1
        failures[qid] = {"category": query.get("category", "unspecified"), "stages": stages}
    return {"schema_version": 1, "cutoff": cutoff, "queries": len(data["queries"]),
            "input_hashes": data["hashes"], "dataset": data["manifest"],
            "paired_comparisons": comparisons,
            "failure_stage_counts": {name: dict(c) for name, c in counts.items()},
            "per_query": failures,
            "phase_latency": {name: distribution([row[name] for row in report[
                "phase_seconds"].values()]) for name in ("lexical", "vector", "fusion")},
            "attribution_scope": "Observable top-k failure stage, not inferred model root "
                "cause. No full candidate-pool capture: cannot separate recall vs cutoff "
                "ranking misses. Vector phase includes query embedding and database access.",
            "statistics_scope": "Paired bootstrap over queries is descriptive. Shared source "
                "documents/question families are correlated; not a production SLA or an "
                "independent benchmark claim. No tuning/resampling in this run.",
            "generation_evaluated": False, "semantic_judge_calls": 0,
            "presentation_budget_applied": False,
            "scope": "Retriever candidates before tool-result token trimming, live source "
                     "visibility checks and body reads; not final model-visible evidence "
                     "or end-to-end Agent task success."}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dataset", type=Path)
    parser.add_argument("report", type=Path)
    parser.add_argument("--split", default="dev")
    parser.add_argument("--cutoff", type=int, default=8)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    raw = args.report.read_bytes()
    result = analyze(json.loads(raw), load(args.dataset, args.split), args.cutoff)
    result["source_report_sha256"] = hashlib.sha256(raw).hexdigest()
    sources = [Path(__file__), Path(__file__).with_name("statistical_metrics.py")]
    result["analysis_code_sha256"] = hashlib.sha256(
        b"".join(p.read_bytes() for p in sources)).hexdigest()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(result["failure_stage_counts"], ensure_ascii=False))


if __name__ == "__main__":
    main()
