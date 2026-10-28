"""Assess a frozen HTTP baseline against the blog's release targets, without model calls.

This is evidence for HTTP preparation quality and efficiency. Browser execution,
approved writes, fault recovery and operational availability need separate evidence.
Missing semantic review, usage or sample size stays unverified.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter, defaultdict
from pathlib import Path
from statistics import median, quantiles

from eval import acceptance, request_metrics, usage
from utils.trace_io import load_trace

PROFILE = Path(__file__).with_name("release_profile.json")


def task_class(tags):
    """Classify evaluation metadata only; never route a live user request."""
    tags = set(tags)
    for name, matches in (
        ("multi", {"multi_intent"}), ("control", {"confirmation"}),
        ("conversation", {"memory", "multi_turn"}), ("prepare", {"write_prepare"}),
        ("browser", {"browser"}), ("article_read", {"body", "comparison", "dependency"}),
        ("read", {"read", "retrieval"}),
    ):
        if tags & matches:
            return name
    return "chat"


def distribution(values, minimum):
    return {"samples": len(values), "p50": median(values) if values else None,
            "p95": quantiles(values, n=100, method="inclusive")[94]
            if len(values) >= minimum else None,
            "max": max(values) if values else None}


def cache_population(doc):
    """Separate actual append-only calls from first calls and changed request layouts."""
    previous, current, previous_usage = None, None, None
    for event in request_metrics.events_of(doc):
        if event.get("event") == "request_layout":
            current = event
        elif event.get("event") == "llm_done":
            label = "unknown_layout"
            if current:
                label = "first_call" if previous is None else "changed_layout"
                if (previous and all(previous.get(key) == current.get(key) for key in
                        ("tools", "model", "session", "tool_choice", "thinking",
                         "reasoning_effort"))
                        and previous.get("messages")
                        and current.get("messages", [])[:len(previous["messages"])]
                        == previous["messages"]):
                    label = "stable_prefix"
            measured = dict(event)
            if label == "stable_prefix" and previous_usage:
                eligible = usage.count(previous_usage.get("input"))
                incoming = usage.count(event.get("input"))
                cached = usage.count(event.get("cache_read"))
                if eligible and incoming is not None and cached is not None and cached <= incoming:
                    measured["known_prefix_input"] = min(eligible, incoming)
                    measured["known_prefix_reused"] = min(eligible, incoming, cached)
            yield label, measured
            previous_usage = event
            previous, current = current, None


def _add_cache(row, event):
    usage.add(row, event)
    for field in ("known_prefix_input", "known_prefix_reused"):
        row[field] = row.get(field, 0) + event.get(field, 0)
    row["known_prefix_samples"] = row.get("known_prefix_samples", 0) + (
        "known_prefix_input" in event)


def _cache_summary(row):
    eligible = row.get("known_prefix_input", 0)
    return {**row, "hit_rate": usage.hit_rate(row),
            "uncached_input": row["cache_in"] - row["cache"] if row["cache_seen"] else None,
            "known_prefix_reuse_rate": row.get("known_prefix_reused", 0) / eligible
            if eligible else None}


def _metrics(report):
    groups, populations = defaultdict(list), defaultdict(usage.empty)
    missing = []
    totals, trace_errors, first_visible = usage.empty(), [], []
    build = (report.get("service_version") or {}).get("sha")
    for case in report["cases"]:
        for index, rnd in enumerate(case.get("result", {}).get("rounds", []), 1):
            doc = load_trace(str(rnd.get("trace", "")))
            if not doc:
                trace_errors.append({"case": case["id"], "round": index, "error": "missing trace"})
                continue
            row = request_metrics.summary(doc)
            if row["errored_calls"]:
                missing.append(f"{case['id']}.{index}: provider usage for errored calls")
            if not build or row["build"] != build:
                trace_errors.append({"case": case["id"], "round": index, "error": "build mismatch"})
            timing = rnd.get("http_timing") or {}
            expected_digest = timing.get("trace_sha256")
            if not expected_digest:
                missing.append(f"{case['id']}.{index}: frozen trace SHA256")
            elif hashlib.sha256(Path(rnd["trace"]).read_bytes()).hexdigest() != expected_digest:
                trace_errors.append({"case": case["id"], "round": index, "error": "trace changed"})
            row.update({key: timing.get(key) for key in
                        ("elapsed_s", "first_command_s", "first_confirmation_s")})
            # Only positive commands/cards have this event. Their absence is judged by gold.
            if timing.get("first_visible_s") is not None:
                first_visible.append(timing["first_visible_s"])
            groups[task_class(case.get("tags", []))].append(row)
            for field in totals:
                totals[field] += row[field]
            for population, event in cache_population(doc):
                _add_cache(populations[population], event)
    return {"groups": groups, "populations": populations, "usage": totals,
            "trace_errors": trace_errors, "first_visible": first_visible,
            "missing": missing}


def _reject(failures, name, passed, detail):
    if not passed:
        failures.append({"gate": name, "detail": detail})


def _quality(report, semantic, profile):
    cases, failures, missing = report["cases"], [], []
    ids = {case["id"] for case in cases}
    required = set(profile["critical_cases"])
    critical = [case for case in cases if case["id"] in required
                or set(case.get("tags", [])) & set(profile["critical_tags"])]
    full = (report.get("full_run") is True and report.get("planned") == len(cases)
            and len(cases) >= profile["min_full_cases"])
    if not full:
        missing.append("complete full baseline with the minimum case count")
    if required - ids:
        missing.append("required critical cases: " + ", ".join(
            sorted(required - ids)))
    _reject(failures, "evaluation_errors", not any(
        case.get("status") == "evaluation_error" for case in cases),
        "HTTP/evidence failures must be resolved before release")
    _reject(failures, "no_online_business_writes", not any(
        any(not row.get("cmd") for row in rnd.get("exec_rows", [])) for case in cases
        for rnd in case.get("result", {}).get("rounds", [])), "this suite must only prepare writes")
    _reject(failures, "deterministic_critical", all(case.get("status") == "passed"
        for case in critical), "critical cases must all pass")
    statuses = Counter(semantic.values())
    core = [case for case in cases if set(case.get("tags", [])) & set(profile["core_tags"])]
    if not semantic or statuses["not_evaluated"]:
        missing.append("complete semantic review tied to the frozen report SHA256")
    if semantic:
        for name, subset, threshold in (("task_quality", cases, profile["task_pass_rate"]),
                                       ("core_quality", core, profile["core_pass_rate"])):
            passed = sum(semantic.get(case["id"]) == "checks_passed" for case in subset)
            rate = passed / len(subset) if subset else 0
            _reject(failures, name, rate >= threshold,
                    {"rate": rate, "target": threshold, "cases": len(subset)})
        _reject(failures, "semantic_critical", all(semantic.get(case["id"]) == "checks_passed"
            for case in critical), "critical cases require deterministic and semantic passes")
    return failures, missing


def _budget_metrics(metrics, profile):
    totals, failures, missing = metrics["usage"], [], []
    coverage = (min(totals["measured"], totals["output_seen"], totals["cache_seen"])
                / totals["calls"] if totals["calls"] else None)
    if coverage is None or coverage < profile["usage_coverage"]:
        missing.append("complete provider usage including cache fields")
    minimum = profile["min_percentile_samples"]
    costs = {}
    for name, limits in profile["task_budgets"].items():
        rows = metrics["groups"][name]
        costs[name] = {}
        for field, limit in limits.items():
            values = [row[field] for row in rows if row.get(field) is not None
                      and (field not in {"in", "out", "calls"} or usage.complete(row))]
            stat = distribution(values, minimum)
            costs[name][field] = {**stat, "target_p95": limit}
            if stat["p95"] is None:
                missing.append(f"{name}.{field}: at least {minimum} measured rounds")
            else:
                _reject(failures, f"{name}.{field}", stat["p95"] <= limit, costs[name][field])
    visible = distribution(metrics["first_visible"], minimum)
    if visible["p95"] is None:
        missing.append("first visible event latency sample size")
    else:
        _reject(failures, "first_visible_s", visible["p95"] <= profile["first_visible_s_p95"],
                visible)
    result = {"task_metrics": costs, "first_visible_s": visible, "usage_coverage": coverage}
    return result, failures, missing


def assess(report_path, *, semantic_path=None, profile_path=PROFILE):
    source, profile_body = Path(report_path).read_bytes(), Path(profile_path).read_bytes()
    report, profile = json.loads(source), json.loads(profile_body)
    ids = [case["id"] for case in report["cases"]]
    if not ids or len(ids) != len(set(ids)):
        raise ValueError("baseline cases must be nonempty and unique")
    if profile.get("version") != 1:
        raise ValueError("unsupported release profile version")
    reviewed = (acceptance.assess(source, json.loads(Path(semantic_path).read_bytes()))
                if semantic_path else None)
    semantic = {case["id"]: case["status"] for case in reviewed["cases"]} if reviewed else {}
    metrics = _metrics(report)
    failures, missing = _quality(report, semantic, profile)
    costs, budget_failures, budget_missing = _budget_metrics(metrics, profile)
    failures.extend(budget_failures)
    missing.extend([*metrics["missing"], *budget_missing])
    profile_digest = hashlib.sha256(profile_body).hexdigest()
    if (report.get("evaluator_sha256") or {}).get("release_profile.json") != profile_digest:
        missing.append("release profile frozen with this baseline")
    _reject(failures, "trace_integrity", not metrics["trace_errors"], metrics["trace_errors"])
    return {
        "scope": profile["scope"], "source_sha256": hashlib.sha256(source).hexdigest(),
        "profile_sha256": profile_digest,
        "status": "failed" if failures else "unverified" if missing else "http_checks_passed",
        "failures": failures, "unverified": missing,
        "semantic_counts": dict(Counter(semantic.values())),
        "deterministic_counts": dict(Counter(case.get("status") for case in report["cases"])),
        "usage": metrics["usage"], **costs,
        "cache_diagnostics": {name: _cache_summary(row)
                              for name, row in metrics["populations"].items()},
        "known_prefix_reuse_target": profile["known_prefix_reuse_target"],
        "separate_evidence_required": ["browser execution and visual experience",
                                       "approved writes, replay and unknown outcomes",
                                       "cancel/resume, concurrent sessions and deletion",
                                       "operational reliability over real observation time"],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path)
    parser.add_argument("--semantic", type=Path)
    parser.add_argument("--profile", type=Path, default=PROFILE)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    result = assess(args.report, semantic_path=args.semantic, profile_path=args.profile)
    args.out.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({key: result[key] for key in ("status", "failures", "unverified")},
                     ensure_ascii=False))
    return 0 if result["status"] == "http_checks_passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
