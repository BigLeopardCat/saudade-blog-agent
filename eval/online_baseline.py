"""First-attempt production HTTP baseline using golden requests and judgments.

Run from the repository root with ``python -m eval.online_baseline``. Preparation
cases never approve writes. Real write/replay contracts run separately against
isolated fixtures; this report does not count them as online write coverage.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from collections import Counter
from datetime import datetime
from pathlib import Path

import httpx

from eval import dataset_audit, request_metrics, task_metrics
from eval.online_transport import EvaluationError, OnlineTransport, safe_result
from utils.trace_io import load_trace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "eval"))
from eval import golden_evaluation as golden  # noqa: E402

SUITE = ROOT / "eval/golden/native_requests.jsonl"
POC_IDS = ("chat_user", "nav_board", "article_discovery", "own_unread",
           "admin_articles", "admin_denial", "tags_add_new", "memory_recall",
           "memory_history_recall", "favorite_prepare")


def fixture_articles(case):
    fixtures = case.get("requires_public_articles", [])
    if ("requires_public_article" in case or not isinstance(fixtures, list)
            or any(type(value) is not int or value <= 0 for value in fixtures)
            or len(set(fixtures)) != len(fixtures)):
        raise ValueError(f"invalid public article fixture: {case['id']}")
    return fixtures


def fixture_images(case):
    images = case.get("requires_gallery_images", [])
    if (not isinstance(images, list)
            or any(type(image) is not int or image <= 0 for image in images)
            or len(set(images)) != len(images)):
        raise ValueError(f"invalid gallery fixture: {case['id']}")
    return images


def _call_limits(gold, case_id):
    """Validate evaluation ceilings before any model or business request."""
    maximum = gold.get("max_model_calls")
    if "max_model_calls" in gold and (type(maximum) is not int or maximum < 0):
        raise ValueError(f"invalid model-call acceptance limit: {case_id}")
    limits = gold.get("max_tool_calls", {})
    if (not isinstance(limits, dict) or any(
            not isinstance(name, str) or not name or type(count) is not int or count < 0
            for name, count in limits.items())):
        raise ValueError(f"invalid tool-call acceptance limit: {case_id}")


def load_suite(path=SUITE):
    cases = [json.loads(line) for line in Path(path).read_text().splitlines() if line.strip()]
    ids = [c["id"] for c in cases]
    if not cases or len(set(ids)) != len(ids):
        raise ValueError("suite is empty or has duplicate IDs")
    for case in cases:
        expectations = [case.get("first_action"), *(case.get("decision_expectations") or [])]
        for value in expectations:
            if value is not None and (not isinstance(value, dict)
                    or set(value) != {"any_tools", "allow_text"}
                    or type(value["allow_text"]) is not bool
                    or not isinstance(value["any_tools"], list)
                    or any(not isinstance(name, str) or not name for name in value["any_tools"])):
                raise ValueError(f"invalid first-action annotation: {case['id']}")
        if "_draft" in case or "draft" in case.get("tags", []):
            raise ValueError(f"unreviewed draft cannot run as a baseline: {case['id']}")
        fixture_articles(case)
        fixture_images(case)
        if case["identity"] not in {"admin", "user", "zako"}:
            raise ValueError(f"unknown identity in {case['id']}")
        for rnd in golden.iter_rounds(case):
            _call_limits(rnd["gold"], case["id"])
            unknown = set(rnd["gold"]) - (golden.GOLD_ASSERT_KEYS | golden.GOLD_REQUEST_KEYS
                                          | golden.GOLD_COMMENT_KEYS | golden.GOLD_ROUND_KEYS)
            if unknown:
                raise ValueError(f"unknown gold keys in {case['id']}: {unknown}")
            if rnd.get("confirm_message"):
                raise ValueError("online preparation suite must not approve business mutations")
            if set(rnd["context"]) & {"history", "executions", "execution_rows", "pending_action",
                                      "summary"}:
                raise ValueError("HTTP suite must establish memory through real turns")
        if ("decision_expectations" in case
                and len(case["decision_expectations"]) != len(golden.iter_rounds(case))):
            raise ValueError(f"decision annotations must cover every declared turn: {case['id']}")
    return cases


def snapshot(folder, cases):
    """Freeze inputs and judgment code before spending any model budget."""
    (folder / "suite.jsonl").write_text("".join(
        json.dumps(case, ensure_ascii=False, separators=(",", ":")) + "\n" for case in cases))
    sources = folder / "evaluator"
    sources.mkdir()
    hashes = {}
    names = ("golden_evaluation.py", "online_baseline.py", "online_transport.py",
                 "stream_protocol.py",
                 "native_assertions.py", "request_metrics.py", "latency_metrics.py",
                 "task_metrics.py", "usage.py",
                 "release_readiness.py", "release_profile.json", "statistical_metrics.py",
                 "action_metrics.py", "dataset_audit.py", "command_report.py", "acceptance.py",
                 "corpus_terms.py", "corpus_provenance.py", "golden_runtime.py",
                 "golden_thread.py", "golden_trace.py", "native_receipt_contract.py",
                 "cache_diagnostics.py")
    paths = {name: ROOT / "eval" / name for name in names}
    paths["agent/contracts/events.py"] = ROOT / "agent/contracts/events.py"
    for name, path in paths.items():
        body = path.read_bytes()
        target = sources / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(body)
        hashes[name] = hashlib.sha256(body).hexdigest()
    return hashes


def build_identity(version, *, expected=None):
    """Workers may start at different times while serving the same build."""
    if not isinstance(version, dict) or not version.get("sha"):
        raise EvaluationError("service version has no build identity")
    if expected is not None and version["sha"] != expected:
        raise EvaluationError("service build differs from the frozen candidate")
    return version["sha"], version.get("version")


def actor_preflight(cases, transport, identities):
    roles = {"admin": {"admin", "superadmin"}, "user": {"user"}, "zako": {"zako"}}
    profiles, allowances = {}, {}
    for key in sorted({case["identity"] for case in cases}):
        uid = identities.get(key, 0)
        if type(uid) is not int or uid <= 0:
            raise ValueError(f"missing real positive account ID for {key}")
        profiles[key] = transport.profile(uid, roles[key])
        if getattr(transport, "host", False):
            required = sum(len(case.get("rounds") or [case])
                           for case in cases if case["identity"] == key)
            allowances[key] = transport.check_turn_allowance(uid, required)
    return profiles, allowances, roles



def check_case_fixtures(case, transport):
    """Read preparation targets; never approve or execute a business mutation."""
    for identifier in fixture_articles(case):
        transport.check_public_article_fixture(identifier)
    for identifier in fixture_images(case):
        transport.check_gallery_fixture(identifier)
    if case.get("requires_quota_usage"):
        transport.check_quota_fixture(case["requires_quota_usage"])
    if case.get("requires_category_creation"):
        transport.check_category_fixture(case["requires_category_creation"])


def fixture_preflight(cases, transport, identities):
    """Validate every fixture before the first model call, retaining per-case results."""
    previous_uid = getattr(transport, "uid", None)
    results = []
    try:
        for case in cases:
            transport.uid = identities[case["identity"]]
            row = {"id": case["id"], "status": "passed"}
            try:
                check_case_fixtures(case, transport)
            except (httpx.HTTPError, EvaluationError, ValueError) as exc:
                row.update(status="evaluation_error", error=f"{type(exc).__name__}: {exc}")
            results.append(row)
    finally:
        transport.uid = previous_uid
    return results


def model_availability_failure(result):
    """Require a failed round and terminal SDK failure facts, never response wording."""
    for round_ in result.get("rounds", []):
        if not round_.get("error"):
            continue
        trace = load_trace(str(round_.get("trace", ""))) or {}
        attempts = [event for event in trace.get("events", [])
                    if event.get("event") in {"llm_start", "llm_done", "llm_error"}]
        final = attempts[-1] if attempts else {}
        if (final.get("event") == "llm_error" and (final.get("retryable") is True
                or final.get("http_status") in {401, 402, 403, 429}
                or final.get("provider_error_code") in {
                    "insufficient_quota", "billing_hard_limit_reached"}
                or final.get("provider_error_type") == "insufficient_quota")
                and final.get("failure_source") in {"provider", "transport"}):
            return {"round": round_.get("round"), "trace": round_.get("trace"),
                    **{key: final[key] for key in (
                        "error_type", "failure_source", "http_status", "retryable", "retry_after_s",
                        "provider_error_type", "provider_error_code", "provider_request_id")
                       if key in final}}
    return None


def run(cases, transport, identities, report_dir, *, phase, suite_hash, include_vision=False,
        dataset_role="regression", expected_build=None, evaluation_id=None):
    excluded = [case for case in cases if not include_vision and "vision" in case["tags"]]
    cases = [case for case in cases if case not in excluded]
    if not cases:
        raise ValueError("no cases remain in the selected evaluation scope")
    folder = Path(report_dir)
    folder.mkdir(parents=True, exist_ok=False, mode=0o700)
    evaluator_hashes = snapshot(folder, cases)
    selected = folder / "suite.jsonl"
    selected.chmod(0o600)
    version = transport.client.get(transport.agent_url + "/version")
    version.raise_for_status()
    initial_version = version.json()
    initial_build = build_identity(initial_version, expected=expected_build)
    profiles, allowances, role_sets = actor_preflight(cases, transport, identities)
    report = {"phase": phase, "suite_sha256": suite_hash, "evaluator_sha256": evaluator_hashes,
              "dataset_role": dataset_role,
              "selected_suite_sha256": hashlib.sha256(selected.read_bytes()).hexdigest(),
              "evaluation_id": evaluation_id,
              "dataset_audit": dataset_audit.audit(cases),
              "service_version": initial_version,
              "excluded_cases": [{"id": case["id"], "reason": "known model vision limitation"}
                                 for case in excluded],
              "include_vision": include_vision,
              "identities": profiles, "actor_allowances": allowances,
              "planned": len(cases), "cases": [], "requests": [],
              "automatic_retries": 0, "business_write_approval": False,
              "retry_scope": "No case resampling; model retry attempts are included in metrics",
              "coverage_limits": ["不批准线上业务写入；实际执行、回放和未知结果由隔离契约评测覆盖",
                                  "自动判据验证行为与部分内容；不等同于完整语义或视觉体验验收",
                                  ("Blog HTTP 包含宿主鉴权和聊天历史；没有模拟浏览器点击"
                                   if getattr(transport, "host", False) else
                                   "Agent HTTP 未建立宿主聊天历史；没有模拟浏览器点击")],
              "started_at": datetime.now().astimezone().isoformat()}

    def save():
        report["requests"] = transport.requests
        report["cleaned_conversations"] = transport.cleaned_conversations
        report["counts"] = dict(Counter(row["status"] for row in report["cases"]))
        paths = [r["trace"] for r in transport.requests]
        report["metrics"] = request_metrics.collect(paths)
        report["task_metrics"] = task_metrics.collect(paths)
        target = folder / "report.json"
        temp = target.with_suffix(".tmp")
        temp.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
        temp.replace(target)

    save()
    preflight = fixture_preflight(cases, transport, identities)
    (folder / "preflight.json").write_text(
        json.dumps(preflight, ensure_ascii=False, indent=2) + "\n")
    if any(row["status"] != "passed" for row in preflight):
        report.update(stopped_reason="fixture preflight failed before any model request",
                      preflight=preflight, full_run=False,
                      finished_at=datetime.now().astimezone().isoformat())
        save()
        return report
    for case in cases:
        before = transport.client.get(transport.agent_url + "/version")
        before.raise_for_status()
        if build_identity(before.json()) != initial_build:
            report["stopped_reason"] = "service version changed during baseline"
            save()
            break
        started = time.monotonic()
        row = {"id": case["id"], "tags": case["tags"], "identity": case["identity"],
               "status_scope": "deterministic_checks_only",
               "acceptance_status": "not_evaluated",
               "case_sha256": hashlib.sha256(json.dumps(case, sort_keys=True).encode()).hexdigest()}
        row.update({key: case[key] for key in ("first_action", "decision_expectations")
                    if key in case})
        try:
            key = case["identity"]
            with transport.conversation(identities[key], role_sets[key]):
                # Preconditions may change after the suite-wide check.
                check_case_fixtures(case, transport)
                result = golden.run_case(case, turn_runner=transport)
                fails = golden.check_case(case, result)
                errors = [r["error"] for r in result["rounds"] if r.get("error")]
                row.update(status="failed" if fails or errors else "passed",
                           fails=fails, errors=errors, result=safe_result(result))
                availability = (result.get("evaluation_failure")
                                or model_availability_failure(result))
                if availability:
                    row.update(status="evaluation_error", evaluation_failure=availability)
        except (httpx.HTTPError, EvaluationError, ValueError) as exc:
            # Evaluation failures stay visible and never become model-quality passes.
            row.update(status="evaluation_error", error=f"{type(exc).__name__}: {exc}")
        row["elapsed_s"] = round(time.monotonic() - started, 3)
        report["cases"].append(row)
        save()
        print(json.dumps({k: row[k] for k in ("id", "status", "elapsed_s")},
                         ensure_ascii=False), flush=True)
        if row.get("evaluation_failure"):
            report["stopped_reason"] = ("captured model availability failure"
                if row["evaluation_failure"]["failure_source"] in {"provider", "transport"}
                else "captured HTTP or evidence failure")
            save()
            break
        if phase == "poc" and row["status"] == "evaluation_error":
            report["stopped_reason"] = "HTTP/evidence precondition failed"
            save()
            break
    report["finished_at"] = datetime.now().astimezone().isoformat()
    report["full_run"] = (len(report["cases"]) == len(cases) and phase == "full"
                          and "stopped_reason" not in report)
    save()
    return report


def select_cases(cases, phase, case_ids=None):
    if case_ids and phase != "poc":
        raise ValueError("--case-id is only for POC; full runs use the complete supplied suite")
    if phase != "poc":
        return cases
    names = case_ids or POC_IDS
    if len(names) != len(set(names)):
        raise ValueError("duplicate case IDs are not separate samples")
    by_id = {case["id"]: case for case in cases}
    if set(names) - by_id.keys():
        raise ValueError("unknown POC case IDs: " + ", ".join(sorted(set(names) - by_id.keys())))
    return [by_id[name] for name in names]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--phase", choices=("poc", "full"), default="poc")
    parser.add_argument("--host", action="store_true", help="Blog HTTP with host persistence")
    parser.add_argument("--include-vision", action="store_true",
                        help="Explicitly include separate model vision diagnostics")
    parser.add_argument("--admin-uid", type=int, default=int(os.getenv("GOLDEN_ADMIN_UID", "0")))
    parser.add_argument("--user-uid", type=int, default=int(os.getenv("GOLDEN_USER_UID", "0")))
    parser.add_argument("--zako-uid", type=int, default=int(os.getenv("GOLDEN_ZAKO_UID", "0")))
    parser.add_argument("--agent-url", default="http://127.0.0.1:8010")
    parser.add_argument("--blog-url", default="http://127.0.0.1:3000")
    parser.add_argument("--report-dir", type=Path, required=True)
    parser.add_argument("--suite", type=Path, default=SUITE)
    parser.add_argument("--case-id", action="append",
                        help="Select a POC case; repeat to preserve order")
    parser.add_argument("--dataset-role", choices=("regression", "holdout"), default="regression")
    parser.add_argument("--expected-build", help="Require this deployed SHA before any model call")
    args = parser.parse_args()
    cases = load_suite(args.suite)
    try:
        cases = select_cases(cases, args.phase, args.case_id)
    except ValueError as error:
        parser.error(str(error))
    transport = OnlineTransport(agent_url=args.agent_url, blog_url=args.blog_url,
                                report_dir=args.report_dir, host=args.host)
    try:
        report = run(cases, transport,
                     {"admin": args.admin_uid, "user": args.user_uid, "zako": args.zako_uid},
                     args.report_dir, phase=args.phase,
                     suite_hash=hashlib.sha256(args.suite.read_bytes()).hexdigest(),
                     include_vision=args.include_vision, dataset_role=args.dataset_role,
                     expected_build=args.expected_build)
    finally:
        transport.client.close()
    complete = len(report["cases"]) == report["planned"]
    return 0 if complete and all(c["status"] == "passed" for c in report["cases"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
