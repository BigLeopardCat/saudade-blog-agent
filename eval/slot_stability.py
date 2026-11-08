"""Read parameter consistency from frozen HTTP reports without running tasks.

Agreement describes repeated observations, not correctness. Missing arguments and
failed scenarios remain in the sample; current golden assertions judge intent.
"""

import argparse
import hashlib
import json
from collections import Counter
from pathlib import Path

MISSING = "(missing)"


def extract_slot(result, tool, field):
    """Retain every matching operation, not only the first card or target."""
    values = []
    for rnd in result.get("rounds", [result]):
        for payload in rnd.get("confirm_payloads", []):
            if not isinstance(payload, dict):
                raise ValueError("invalid confirmation evidence")
            for spec in payload.get("specs", []):
                if spec.get("tool") != tool:
                    continue
                args = spec.get("args")
                if not isinstance(args, dict) or field not in args:
                    return None
                values.append(args[field])
    return json.dumps(values, ensure_ascii=False, sort_keys=True) if values else None


def summarize(values):
    counts = Counter(values)
    observed = [(value, count) for value, count in counts.items() if value != MISSING]
    mode, frequency = max(observed, key=lambda row: row[1]) if observed else (None, 0)
    return {"trials": len(values), "values": dict(counts), "mode": mode,
            "mode_n": frequency, "agreement": frequency / len(values) if values else None,
            "missing_n": counts.get(MISSING, 0)}


def _identity(report):
    metrics = report.get("metrics")
    if not isinstance(metrics, dict):
        raise ValueError("report lacks measured model identity")
    groups = metrics.get("groups")
    if not isinstance(groups, list) or not groups:
        raise ValueError("report lacks measured model identity")
    if any(not isinstance(row, dict) for row in groups):
        raise ValueError("invalid measured model identity")
    models = {(row.get("source"), row.get("build"), row.get("model")) for row in groups}
    if any(not isinstance(value, str) or not value or value == "unknown"
           for row in models for value in row):
        raise ValueError("unknown model or build in report")
    if len(models) != 1:
        raise ValueError("report mixes measured model identities")
    models = sorted(models)
    required = (report.get("suite_sha256"), report.get("selected_suite_sha256"),
                (report.get("service_version") or {}).get("sha"), report.get("evaluator_sha256"))
    if not all(required):
        raise ValueError("report lacks frozen evaluation identity")
    if not isinstance(required[2], str) or models[0][1] != required[2]:
        raise ValueError("measured build differs from service version")
    return json.dumps([*required, models], sort_keys=True)


def collect(paths, *, case_id, tool, field):
    identity, seen, draws = None, set(), []
    for source in paths:
        body = Path(source).read_bytes()
        digest = hashlib.sha256(body).hexdigest()
        if digest in seen:
            raise ValueError("duplicate report evidence")
        seen.add(digest)
        report = json.loads(body)
        current = _identity(report)
        if identity is not None and identity != current:
            raise ValueError("cannot mix builds, models, suites or evaluators")
        identity = current
        cases = report.get("cases", [])
        if len({case["id"] for case in cases}) != len(cases):
            raise ValueError("duplicate scenario in report")
        row = next((case for case in cases if case["id"] == case_id), None)
        if row is None:
            raise ValueError("requested scenario absent from report")
        if row.get("status") not in {"passed", "failed", "evaluation_error"}:
            raise ValueError("unknown scenario status")
        value = extract_slot(row.get("result") or {}, tool, field)
        draws.append({"report": str(source), "sha256": digest, "status": row["status"],
                      "value": value if value is not None else MISSING})
    if not draws:
        raise ValueError("no reports supplied")
    return {"case": case_id, "slot": f"{tool}.{field}", "draws": draws,
            **summarize([row["value"] for row in draws]),
            "failed_trials": sum(row["status"] != "passed" for row in draws),
            "interpretation": "Consistency is not task correctness or release approval."}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("reports", nargs="+", type=Path)
    parser.add_argument("--case", required=True, dest="case_id")
    parser.add_argument("--tool", required=True)
    parser.add_argument("--field", required=True)
    args = parser.parse_args()
    print(json.dumps(collect(args.reports, case_id=args.case_id, tool=args.tool, field=args.field),
                     ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
