"""Connect frozen candidates to existing evaluation and a manual release review."""

import json
from pathlib import Path

from eval import acceptance, compare_runs, judge_calibration, release_readiness
from eval.improvement.artifacts import digest, nonempty
from eval.improvement.datasets import scope_issues, suite_bytes
from eval.improvement.holdout import completed
from eval.statistical_metrics import proportion
from utils.trace_io import load_trace


def read(path):
    return json.loads(Path(path).read_bytes())


def verify_run(report, cases, build, role):
    expected = {case["id"]: digest(json.dumps(case, sort_keys=True).encode()) for case in cases}
    actual = {case["id"]: case.get("case_sha256") for case in report["cases"]}
    if (report.get("full_run") is not True or report.get("planned") != len(cases)
            or len(report["cases"]) != len(cases) or expected != actual
            or report.get("suite_sha256") != digest(suite_bytes(cases))
            or report.get("dataset_role") != role
            or report.get("service_version", {}).get("sha") != build
            or report.get("business_write_approval") is not False
            or report.get("automatic_retries") != 0):
        raise ValueError("run does not match frozen candidate, dataset or safe HTTP scope")
    for case in report["cases"]:
        for rnd in case.get("result", {}).get("rounds", []):
            doc = load_trace(str(rnd.get("trace", "")))
            if (not doc or doc.get("input", {}).get("runtime", {}).get("build_sha") != build
                    or rnd.get("exec_rows")):
                raise ValueError("trace build or preparation-only boundary mismatch")


def semantic(path, report_path):
    doc = read(path)
    if not doc.get("rubric_sha256") or not doc.get("evaluator_sha256"):
        raise ValueError("semantic review needs frozen rubric and evaluator hashes")
    return acceptance.assess(Path(report_path).read_bytes(), doc)


def archive_run(registry, report, review):
    report_id, semantic_id = registry.evidence(report), registry.evidence(review)
    traces = {}
    for case in read(report)["cases"]:
        for rnd in case.get("result", {}).get("rounds", []):
            path = rnd["trace"]
            if path not in traces:
                traces[path] = registry.evidence(path)
    return {"report": report_id, "semantic": semantic_id, "traces": traces}


def development(registry, candidate_id, before, after, before_semantic, after_semantic,
                *, reviewer, comparison_conditions, calibration_path):
    nonempty(reviewer, "reviewer")
    nonempty(comparison_conditions, "comparison_conditions")
    candidate = registry.get(candidate_id, "candidate")
    split = registry.get(candidate["datasets"], "datasets")
    if digest(release_readiness.PROFILE.read_bytes()) != split["profile_sha256"]:
        raise ValueError("release targets changed after dataset sealing")
    left, right = read(before), read(after)
    verify_run(left, split["development"], candidate["base"], "regression")
    verify_run(right, split["development"], candidate["head"], "regression")
    if (not left.get("evaluator_sha256")
            or left["evaluator_sha256"] != right.get("evaluator_sha256")):
        raise ValueError("paired evaluator definitions changed")
    a, b = read(before_semantic), read(after_semantic)
    if any(a.get(key) != b.get(key) for key in ("rubric_sha256", "evaluator_sha256", "model")):
        raise ValueError("paired semantic reviewer definitions changed")
    calibration = read(calibration_path)
    if (calibration.get("model") != b.get("model")
            or calibration.get("rubric_sha256") != b.get("rubric_sha256")
            or not calibration.get("source_sha256") or not calibration.get("rows")):
        raise ValueError("judge calibration does not match the semantic reviewer")
    calibration_result = judge_calibration.summarize(calibration["rows"])
    if (calibration_result["unverified"] or not calibration_result["recall"]["total"]
            or not calibration_result["false_positive_rate"]["total"]):
        raise ValueError("judge calibration needs measured positive and negative samples")
    old, new = semantic(before_semantic, before), semantic(after_semantic, after)
    previous = {row["id"]: row["status"] for row in old["cases"]}
    regressions = [row["id"] for row in new["cases"]
                   if previous[row["id"]] == "checks_passed" and row["status"] != "checks_passed"]
    paired = compare_runs.compare(left, right)
    readiness = release_readiness.assess(after, semantic_path=after_semantic)
    cost_ok = all((paired["cost"][key]["paired_bootstrap_95"] or [None, None])[1] is not None
                  and paired["cost"][key]["paired_bootstrap_95"][1] <= 0
                  for key in ("uncached_input", "calls"))
    passed = (readiness["status"] == "http_checks_passed" and cost_ok
              and not paired["regressions"] and not regressions)
    return registry.put("development", {
        "candidate": candidate_id, "status": "passed" if passed else "blocked",
        "reviewer": reviewer, "comparison_conditions": comparison_conditions,
        "paired": paired, "readiness": readiness, "semantic_regressions": regressions,
        "cost_nonincrease_supported": cost_ok,
        "calibration": registry.evidence(calibration_path),
        "calibration_results": calibration_result,
        "baseline": archive_run(registry, before, before_semantic),
        "candidate_run": archive_run(registry, after, after_semantic),
        "semantic_definition": {key: b.get(key) for key in
                                ("rubric_sha256", "evaluator_sha256", "model")},
        "evaluator_sha256": right["evaluator_sha256"]})


def release_packet(registry, reservation, report_path, semantic_path, *, reviewer):
    nonempty(reviewer, "reviewer")
    claim = registry.get(reservation, "reservation")
    gate = registry.get(claim["development"], "development")
    candidate = registry.get(gate["candidate"], "candidate")
    split = registry.get(candidate["datasets"], "datasets")
    if gate["status"] != "passed" or claim["head"] != candidate["head"]:
        raise ValueError("holdout reservation does not bind a passed frozen candidate")
    if digest(release_readiness.PROFILE.read_bytes()) != split["profile_sha256"]:
        raise ValueError("release targets changed after dataset sealing")
    completed(registry, reservation, report_path)
    report, review = read(report_path), read(semantic_path)
    if report.get("evaluation_id") != reservation:
        raise ValueError("holdout report lacks its bound reservation ID")
    verify_run(report, split["holdout"], candidate["head"], "holdout")
    if (report.get("evaluator_sha256") != gate["evaluator_sha256"]
            or any(review.get(key) != value for key, value in gate["semantic_definition"].items())):
        raise ValueError("holdout evaluator differs from development evaluation")
    combined = semantic(semantic_path, report_path)
    # Reuse trace hash and complete-usage validation; comparing a run to itself
    # is only an integrity check, never evidence of improvement.
    compare_runs.compare(report, report)
    missing = scope_issues(split["holdout"])
    passed = sum(row["status"] == "checks_passed" for row in combined["cases"])
    if passed != len(split["holdout"]):
        missing.append("holdout has failed, suspect or unevaluated outcomes")
    packet = {
        "status": "blocked" if missing else "ready_for_review", "candidate": gate["candidate"],
        "head": candidate["head"], "rollback_base": candidate["base"],
        "reservation": reservation, "reviewer": reviewer, "unresolved": sorted(missing),
        "holdout_statistics": proportion(passed, len(split["holdout"])),
        "holdout_evidence": archive_run(registry, report_path, semantic_path),
        "separate_evidence_required": gate["readiness"]["separate_evidence_required"],
        "scope": "HTTP preparation review packet; no merge, deploy or business approval",
        "limitations": split["limits"] + ["Judge calibration is not production accuracy.",
            "After exposure this holdout must not select or tune another candidate."]}
    return registry.put("release_packet", packet)
