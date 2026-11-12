"""Known-source single-candidate input visibility audit; no reranker inference or answers."""

from __future__ import annotations

import argparse
import gzip
import json
import lzma
import os
from functools import lru_cache
from pathlib import Path

os.environ["SAUDADE_IGNORE_ENV_FILE"] = "1"

from eval.offline_rag_eval import evaluate  # noqa: E402
from eval.rag_dataset import load  # noqa: E402
from eval.reranker_inputs import PairEncoder, sha256  # noqa: E402
from eval.retrieval_evidence import frozen_labels  # noqa: E402
from rag.sections import outline  # noqa: E402


def audit(data, before, encoder):
    if before["input_hashes"] != data["hashes"]:
        raise ValueError("known-source budget audit dataset changed")
    original = before["arms"]["hybrid"]
    visible, pairs = {}, {}

    @lru_cache(maxsize=1024)
    def sections(did):
        row = data["corpus"][did]
        return {s["section_id"]: s["text"] for s in outline(row["text"], row.get("title", ""))}

    for qid, hits in original["locations"].items():
        if len(hits) != 1 or len(original["run"][qid]) != 1:
            raise ValueError("this audit requires the frozen single-candidate scope")
        did, match = hits[0]["document_id"], hits[0]["matches"][0]
        text = sections(did)[match["section_id"]][match["offset"]:match["end"]]
        pair = encoder.encode(data["queries"][qid]["text"],
                              data["corpus"][did].get("title", ""), text, match)
        visible[qid] = [{"document_id": did, "matches": pair["visible_source"]}]
        pairs[qid] = {k: v for k, v in pair.items() if k != "input_ids"}
    scored = evaluate(data, original["run"], [1, 3, 8], locations=visible)
    baseline = evaluate(data, original["run"], [1, 3, 8], locations=original["locations"])
    return {"dataset": data["manifest"], "input_hashes": data["hashes"],
        "evaluation_labels": frozen_labels(data),
        "original": {**baseline, "locations": original["locations"]},
        "reranker_input": {**scored, "locations": visible}, "input_audits": pairs,
        "queries": len(pairs), "truncated_pairs": sum(p["truncated"] for p in pairs.values()),
        "model_calls": 0, "semantic_judge_calls": 0,
        "scope": "Known-source single candidate cannot test ordering. Only the cross-encoder "
                 "input visibility is audited; original Agent read locators remain unchanged. "
                 "Missing evidence in this scoring input is not an Agent task failure."}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("dataset", "baseline", "tokenizer", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    args = parser.parse_args()
    data = load(args.dataset, "dev")
    with gzip.open(args.baseline, "rt") as stream:
        before = json.load(stream)
    report = audit(data, before, PairEncoder(args.tokenizer))
    report["tokenizer_sha256"] = sha256(args.tokenizer)
    report["baseline_sha256"] = sha256(args.baseline)
    with lzma.open(args.output, "wt", encoding="utf-8") as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2)
    print(json.dumps({"queries": report["queries"], "truncated_pairs": report["truncated_pairs"],
                      "input_coverage": report["reranker_input"]["summary"]["all"]
                      ["PassageEvidenceCoverage@8"]["value"]}), flush=True)


if __name__ == "__main__":
    main()
