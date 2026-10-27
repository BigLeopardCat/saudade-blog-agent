"""Combine frozen deterministic and semantic checks without claiming task completion."""

import hashlib
import json
from collections import Counter


def assess(source: bytes, semantic: dict) -> dict:
    if semantic.get("source_report_sha256") != hashlib.sha256(source).hexdigest():
        raise ValueError("semantic report does not belong to this frozen source")
    cases = json.loads(source)["cases"]
    expected = {case["id"]: case for case in cases}
    if not cases or len(expected) != len(cases):
        raise ValueError("source cases must be nonempty and unique")
    rows = {}
    for row in semantic["rows"]:
        key = row["case"], row["round"]
        if key in rows or key[0] not in expected:
            raise ValueError("duplicate or unknown semantic result")
        rounds = expected[key[0]].get("result", {}).get("rounds", [])
        if type(key[1]) is not int or not 1 <= key[1] <= max(1, len(rounds)):
            raise ValueError("semantic result has an unexpected round")
        rows[key] = row
    results = []
    for case in cases:
        rounds = case.get("result", {}).get("rounds", [])
        states = [rows.get((case["id"], index + 1), {}).get("status")
                  for index in range(len(rounds))]
        status = "not_evaluated"
        if case["status"] == "failed":
            status = "failed"
        elif case["status"] == "passed" and rounds:
            if "suspect" in states:
                status = "needs_review"
            elif all(state == "no_issue_found" for state in states):
                status = "checks_passed"
        results.append({"id": case["id"], "status": status,
                        "deterministic_status": case["status"],
                        "reviewed_rounds": sum(state == "no_issue_found" for state in states),
                        "expected_rounds": len(rounds)})
    return {"source_report_sha256": semantic["source_report_sha256"],
            "scope": "recorded HTTP responses and proposed actions; "
                     "browser and post-approval outcomes excluded",
            "cases": results, "counts": dict(Counter(row["status"] for row in results))}
