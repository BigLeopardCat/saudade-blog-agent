"""Read-only inventory of task coverage, exact duplicates and decision annotations."""

import argparse
import json
from collections import Counter, defaultdict
from pathlib import Path


def audit(cases):
    inputs = defaultdict(list)
    total_rounds, annotated = 0, 0
    for case in cases:
        rounds = case.get("rounds") or [case]
        total_rounds += len(rounds)
        labels = case.get("decision_expectations")
        annotated += (sum(label is not None for label in labels) if labels
                      else bool(case.get("first_action")))
        body = {"identity": case["identity"], "context": case.get("context"),
                "turns": [{key: rnd.get(key) for key in ("user_input", "context", "image")}
                          for rnd in rounds]}
        inputs[json.dumps(body, ensure_ascii=False, sort_keys=True)].append(case["id"])
    return {"cases": len(cases), "rounds": total_rounds, "annotated_decisions": annotated,
            "separate_vision_cases": [case["id"] for case in cases
                                      if "vision" in case.get("tags", [])],
            "unannotated_decisions": total_rounds - annotated,
            "identities": dict(Counter(case["identity"] for case in cases)),
            "tags": dict(Counter(tag for case in cases for tag in case.get("tags", []))),
            "exact_duplicate_inputs": [ids for ids in inputs.values() if len(ids) > 1],
            "limitations": ["Exact deduplication does not detect semantic paraphrases.",
                            "Tag coverage does not establish real traffic representativeness.",
                            "A holdout label does not prove it was unseen during tuning."]}


def main():
    from eval.online_baseline import SUITE, load_suite

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suite", type=Path, default=SUITE)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    result = audit(load_suite(args.suite))
    args.out.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
