"""Recompute frozen ranking, evidence and visible-frame estimates without source bodies/models."""

from __future__ import annotations

import argparse
import gzip
import json
import lzma
from pathlib import Path

from langchain_core.messages import ToolMessage

from agent.runtime.message_content import estimate_tokens
from eval.offline_rag_eval import evaluate
from eval.replay_retrieval_ranks import scalar_metrics
from eval.section_range_ablation import volume


def metrics(data, expected, cutoffs, label, locations=None):
    actual = evaluate(data, expected["run"], cutoffs, locations=locations)
    if actual["per_query"].keys() != expected["per_query"].keys():
        raise ValueError("frozen query population changed: " + label)
    for qid, values in expected["per_query"].items():
        scalar_metrics(actual["per_query"][qid], values, label + "/" + qid)
    for group, values in expected["summary"].items():
        for name, value in values.items():
            scalar_metrics(actual["summary"][group][name], value, label + "/" + name)
    return actual


def costs(arm, label):
    values = arm["per_query_cost"]
    if values.keys() != arm["run"].keys() or values.keys() != arm["per_query_frame_lengths"].keys():
        raise ValueError("frozen cost population changed")
    for qid, expected in values.items():
        lengths = arm["per_query_frame_lengths"][qid]
        if any(type(size) is not int or size <= 0 for size in lengths):
            raise ValueError("invalid frozen frame length")
        tokens = sum(estimate_tokens([ToolMessage(content="x" * size,
            tool_call_id="context-read")]) for size in lengths)
        hits = arm["locations"][qid]
        chars = sum(m["end"] - m["offset"] for hit in hits for m in hit["matches"])
        reads = sum(len(hit["matches"]) for hit in hits)
        if reads != len(lengths) or tokens > arm["policy"]["query_frame_estimated_token_budget"]:
            raise ValueError("frozen read count or context budget changed")
        scalar_metrics(expected, {"frame_estimated_tokens": tokens, "body_chars": chars,
            "unique_source_chars": volume(hits), "simulated_reads": reads}, label + "/" + qid)
    means = {name: sum(row[name] for row in values.values()) / len(values)
             for name in arm["mean_cost"]}
    scalar_metrics(means, arm["mean_cost"], label + "/mean_cost")


def replay(report):
    result = {}
    for dataset in ("du", "cmrc"):
        group = report[dataset]
        data = {**group["evaluation_labels"], "manifest": group["dataset"],
                "hashes": group["input_hashes"]}
        for name, arm in group["documents"].items():
            actual = metrics(data, arm, [1, 3, 8, 24, 100], dataset + "/" + name)
            for cohort, expected in group["groups"][name].items():
                qids = group["fixed_group_query_ids"][cohort]
                mean = {m: sum(actual["per_query"][qid][m] for qid in qids) / len(qids)
                        for m in expected}
                scalar_metrics(mean, expected, dataset + "/" + cohort)
        for name, arm in group["contexts"].items():
            metrics(data, arm, [1, 3, 8], dataset + "/" + name, arm["locations"])
            costs(arm, dataset + "/" + name)
        result[dataset] = {"queries": len(data["queries"]),
                           "ranking_arms": len(group["documents"]),
                           "reading_policies": len(group["contexts"])}
    return {"datasets": result, "model_calls": 0, "database_calls": 0,
            "scope": "Frozen ranks, coordinate labels and observed final-frame lengths. "
                     "BGE body tokenization and source parsing need the matching public source; "
                     "their recorded averages are checked, not re-tokenized here."}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path)
    args = parser.parse_args()
    opener = lzma.open if args.report.suffix == ".xz" else gzip.open
    with opener(args.report, "rt", encoding="utf-8") as stream:
        report = json.load(stream)
    print(json.dumps(replay(report), ensure_ascii=False))


if __name__ == "__main__":
    main()
