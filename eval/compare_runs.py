"""Pair frozen runs by complete case; never treat turns as independent tasks."""

import argparse
import hashlib
import json
from pathlib import Path

from eval import request_metrics, usage
from eval.statistical_metrics import paired_delta, paired_outcomes
from utils.trace_io import load_trace


def _cases(report):
    cases = {case["id"]: case for case in report["cases"]}
    if len(cases) != len(report["cases"]) or not cases or not report.get("full_run"):
        raise ValueError("comparison requires complete frozen runs with unique cases")
    if any(case["status"] not in {"passed", "failed"} for case in cases.values()):
        raise ValueError("evaluation errors must be resolved before paired comparison")
    return cases


def _cost(case):
    rows = []
    for rnd in case["result"]["rounds"]:
        expected = (rnd.get("http_timing") or {}).get("trace_sha256")
        if not expected or hashlib.sha256(Path(rnd["trace"]).read_bytes()).hexdigest() != expected:
            raise ValueError("paired trace integrity is missing or changed")
        doc = load_trace(str(rnd["trace"]))
        if doc is None:
            raise ValueError("missing trace in paired evidence")
        row = request_metrics.summary(doc)
        if not usage.complete(row) or row["errored_calls"]:
            raise ValueError("paired cost needs complete provider usage")
        rows.append(row)
    return {"input": sum(row["in"] for row in rows),
            "uncached_input": sum(row["in"] - row["cache"] for row in rows),
            "output": sum(row["out"] for row in rows),
            "calls": sum(row["calls"] for row in rows)}


def compare(before, after):
    left, right = _cases(before), _cases(after)
    if before.get("suite_sha256") != after.get("suite_sha256") or set(left) != set(right):
        raise ValueError("paired runs must use the same frozen suite and case IDs")
    for name in left:
        if (not left[name].get("case_sha256")
                or left[name]["case_sha256"] != right[name].get("case_sha256")):
            raise ValueError("paired case inputs or assertions changed")
    names = sorted(left)
    costs = [(_cost(left[name]), _cost(right[name])) for name in names]
    return {"scope": "deterministic checks and token usage; semantic quality not inferred",
            "unit": "complete scenario, including all its turns",
            "quality": paired_outcomes([left[name]["status"] == "passed" for name in names],
                                       [right[name]["status"] == "passed" for name in names]),
            "regressions": [name for name in names if left[name]["status"] == "passed"
                            and right[name]["status"] == "failed"],
            "cost": {key: paired_delta([pair[0][key] for pair in costs],
                                       [pair[1][key] for pair in costs])
                     for key in ("input", "uncached_input", "output", "calls")},
            "limitations": ["Curated cases do not establish a production traffic distribution.",
                            "Cache warmness and gateway load must be controlled."]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("before", type=Path)
    parser.add_argument("after", type=Path)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    result = compare(json.loads(args.before.read_bytes()), json.loads(args.after.read_bytes()))
    args.out.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
