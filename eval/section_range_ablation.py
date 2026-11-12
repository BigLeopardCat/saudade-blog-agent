"""Replay returned sections as full source ranges, without changing selection or calling models."""

from __future__ import annotations

import argparse
import copy
import gzip
import hashlib
import json
from pathlib import Path

from eval.offline_rag_eval import evaluate
from eval.rag_dataset import load
from eval.replay_retrieval_ranks import scalar_metrics
from rag.sections import outline


def volume(hits):
    grouped = {}
    for hit in hits:
        for match in hit["matches"]:
            grouped.setdefault((hit["document_id"], match["section_id"]), []).append(
                (match["offset"], match["end"]))
    total = 0
    for spans in grouped.values():
        end = 0
        for low, high in sorted(spans):
            total += max(0, high - max(low, end))
            end = max(end, high)
    return total


def expand(locations, sections):
    result = copy.deepcopy(locations)
    for hits in result.values():
        for hit in hits:
            source = sections[hit["document_id"]]
            for match in hit["matches"]:
                size = len(source[match["section_id"]])
                if not 0 <= match["offset"] < match["end"] <= size:
                    raise ValueError("returned range outside pinned section")
            ids = list(dict.fromkeys(match["section_id"] for match in hit["matches"]))
            hit["matches"] = [{"section_id": sid, "offset": 0, "end": len(source[sid])}
                              for sid in ids]
    return result


def compare(data, report, cutoff=8):
    if report["input_hashes"] != data["hashes"]:
        raise ValueError("section ablation source binding changed")
    if cutoff not in report["config"]["cutoffs"]:
        raise ValueError("ablation cutoff not evaluated in original report")
    sections = {did: {s["section_id"]: s["text"] for s in outline(
        row["text"], row.get("title", ""))} for did, row in data["corpus"].items()}
    results = {}
    for name, arm in report["arms"].items():
        original = evaluate(data, arm["run"], report["config"]["cutoffs"],
                            locations=arm["locations"])
        for qid, metrics in arm["per_query"].items():
            scalar_metrics(original["per_query"][qid], metrics, name + "/" + qid)
        expanded = expand(arm["locations"], sections)
        rescored = evaluate(data, arm["run"], report["config"]["cutoffs"], locations=expanded)
        metric = f"PassageEvidenceCoverage@{cutoff}"
        recovered = [qid for qid, metrics in arm["per_query"].items()
                     if metric in metrics and metrics[metric] < 1
                     and rescored["per_query"][qid][metric] == 1]
        results[name] = {"original": original["summary"],
            "same_returned_sections_expanded": rescored["summary"],
            "original_mean_fetchable_chars": sum(volume(h[:cutoff]) for h in
                arm["locations"].values()) / len(data["queries"]),
            "expanded_mean_fetchable_chars": sum(volume(h[:cutoff]) for h in
                expanded.values()) / len(data["queries"]),
            "range_misses_recovered_by_same_section": len(recovered),
            "recovered_query_ids": recovered}
    return {"input_hashes": data["hashes"], "queries": len(data["queries"]),
        "cutoff": cutoff, "arms": results, "model_calls": 0, "database_calls": 0,
        "scope": "Counterfactual source ranges with identical document/section choices. "
                 "More fetchable text, not equal token budget, actual body read or answer "
                 "correctness. Known source scope is not global retrieval. "
                 "Do not count range misses as failed Agent tasks."}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dataset", type=Path)
    parser.add_argument("report", type=Path)
    parser.add_argument("--split", default="dev")
    parser.add_argument("--cutoff", type=int, default=8)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    opener = gzip.open if args.report.suffix == ".gz" else open
    with opener(args.report, "rt", encoding="utf-8") as stream:
        report = json.load(stream)
    result = compare(load(args.dataset, args.split), report, args.cutoff)
    result["source_sha256"] = hashlib.sha256(args.report.read_bytes()).hexdigest()
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")


if __name__ == "__main__":
    main()
