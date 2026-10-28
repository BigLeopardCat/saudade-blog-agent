"""Freeze separated datasets and record one final exposure per held-out content."""

import json

from eval import run_golden
from eval.improvement.artifacts import digest, encode, nonempty, private_write
from eval.improvement.feedback import validate_cases
from eval.online_baseline import load_suite
from eval.release_readiness import PROFILE


def suite_bytes(cases):
    return "".join(json.dumps(case, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                   + "\n" for case in cases).encode()


def requests(cases):
    return {"".join(rnd["user_input"].split()).casefold()
            for case in cases for rnd in run_golden.iter_rounds(case)}


def groups(cases):
    return {nonempty(case.get("source_group"), "source_group") for case in cases}


def scope_issues(cases):
    profile = json.loads(PROFILE.read_bytes())
    tags = {tag for case in cases for tag in case["tags"]}
    missing = []
    if len(cases) < profile["min_full_cases"]:
        missing.append(f"holdout needs at least {profile['min_full_cases']} scenarios")
    for tag in {"chat", "read", "write_prepare", "memory", *profile["critical_tags"]} - tags:
        missing.append("holdout coverage: " + tag)
    if {case["identity"] for case in cases} != {"admin", "user", "zako"}:
        missing.append("holdout coverage: all supported roles")
    return missing


def seal(registry, development, holdout, *, curator, independence_review):
    nonempty(curator, "curator")
    nonempty(independence_review, "independence_review")
    dev, held = validate_cases(load_suite(development)), validate_cases(load_suite(holdout))
    if ({case["id"] for case in dev} & {case["id"] for case in held}
            or requests(dev) & requests(held) or groups(dev) & groups(held)):
        raise ValueError("development and holdout overlap by ID, request or source group")
    if any("vision" in case["tags"] for case in held):
        raise ValueError("known unsupported vision belongs in a separate evaluation")
    key = registry.put("datasets", {
        "development": dev, "holdout": held,
        "development_sha256": digest(suite_bytes(dev)),
        "holdout_sha256": digest(suite_bytes(held)),
        "exposure_key": digest(encode(sorted(requests(held)))),
        "profile_sha256": digest(PROFILE.read_bytes()),
        "curator": curator, "independence_review": independence_review,
        "limits": ["Exact-match checks cannot detect all semantic paraphrases.",
                   "Curator must review source lineage and keep holdout away from tuning.",
                   "Local operator can read files; this is not an access-control enclave."]})
    private_write(registry.root / "datasets" / key / "development.jsonl", suite_bytes(dev))
    # Do not materialize the holdout runner input until development evidence passes.
    return key


def reserve(registry, development):
    gate = registry.get(development, "development")
    if gate["status"] != "passed":
        raise ValueError("development checks have not passed")
    candidate = registry.get(gate["candidate"], "candidate")
    datasets = registry.get(candidate["datasets"], "datasets")
    missing = scope_issues(datasets["holdout"])
    if missing:
        raise ValueError("holdout policy incomplete: " + "; ".join(sorted(missing)))
    claim = {"development": development, "head": candidate["head"],
             "datasets": candidate["datasets"], "holdout_sha256": datasets["holdout_sha256"]}
    # Exclusive creation is the claim; a crash also consumes this exposure.
    private_write(registry.root / "exposures" / (datasets["exposure_key"] + ".json"),
                  json.dumps(claim).encode())
    # Also reject a renamed or partly overlapping replacement suite, including
    # paraphrases from a source group that already supplied final-test evidence.
    units = {*("request:" + text for text in requests(datasets["holdout"])),
             *("group:" + group for group in groups(datasets["holdout"]))}
    for unit in sorted(units):
        private_write(registry.root / "exposures" / "units" / (digest(unit.encode()) + ".json"),
                      json.dumps(claim).encode())
    key = registry.put("reservation", claim)
    private_write(registry.root / "datasets" / candidate["datasets"] / "holdout.jsonl",
                  suite_bytes(datasets["holdout"]))
    return key
