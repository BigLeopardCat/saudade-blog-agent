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

from eval import request_metrics
from eval.online_transport import EvaluationError, OnlineTransport, safe_result

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "eval"))
import run_golden as golden  # noqa: E402

SUITE = ROOT / "eval/golden/native_requests.jsonl"
POC_IDS = ("chat_user", "nav_board", "article_discovery", "own_unread",
           "admin_articles", "admin_denial", "tags_add_new", "memory_recall")


def load_suite(path=SUITE):
    cases = [json.loads(line) for line in Path(path).read_text().splitlines() if line.strip()]
    ids = [c["id"] for c in cases]
    if not cases or len(set(ids)) != len(ids):
        raise ValueError("suite is empty or has duplicate IDs")
    for case in cases:
        if "_draft" in case or "draft" in case.get("tags", []):
            raise ValueError(f"unreviewed draft cannot run as a baseline: {case['id']}")
        if case["identity"] not in {"admin", "user", "zako"}:
            raise ValueError(f"unknown identity in {case['id']}")
        for rnd in golden.iter_rounds(case):
            unknown = set(rnd["gold"]) - (golden.GOLD_ASSERT_KEYS | golden.GOLD_REQUEST_KEYS
                                          | golden.GOLD_COMMENT_KEYS | golden.GOLD_ROUND_KEYS)
            if unknown:
                raise ValueError(f"unknown gold keys in {case['id']}: {unknown}")
            if rnd.get("confirm_message"):
                raise ValueError("online preparation suite must not approve business mutations")
            if set(rnd["context"]) & {"history", "executions", "execution_rows", "pending_action",
                                      "agent_tasks", "summary"}:
                raise ValueError("HTTP suite must establish memory through real turns")
    return cases


def snapshot(folder, cases):
    """Freeze inputs and judgment code before spending any model budget."""
    (folder / "suite.jsonl").write_text("".join(
        json.dumps(case, ensure_ascii=False, separators=(",", ":")) + "\n" for case in cases))
    sources = folder / "evaluator"
    sources.mkdir()
    hashes = {}
    for name in ("run_golden.py", "online_baseline.py", "online_transport.py",
                 "native_assertions.py", "request_metrics.py", "usage.py"):
        body = (ROOT / "eval" / name).read_bytes()
        (sources / name).write_bytes(body)
        hashes[name] = hashlib.sha256(body).hexdigest()
    return hashes


def build_identity(version):
    """Workers may start at different times while serving the same build."""
    if not isinstance(version, dict) or not version.get("sha"):
        raise EvaluationError("service version has no build identity")
    return version["sha"], version.get("version")


def run(cases, transport, identities, report_dir, *, phase, suite_hash, include_vision=False):
    excluded = [case for case in cases if not include_vision and "vision" in case["tags"]]
    cases = [case for case in cases if case not in excluded]
    if not cases:
        raise ValueError("no cases remain in the selected evaluation scope")
    folder = Path(report_dir)
    folder.mkdir(parents=True, exist_ok=False, mode=0o700)
    evaluator_hashes = snapshot(folder, cases)
    version = transport.client.get(transport.agent_url + "/version")
    version.raise_for_status()
    initial_version = version.json()
    initial_build = build_identity(initial_version)
    required = {c["identity"] for c in cases}
    role_sets = {"admin": {"admin", "superadmin"}, "user": {"user"}, "zako": {"zako"}}
    profiles = {}
    for key in required:
        uid = identities.get(key, 0)
        if type(uid) is not int or uid <= 0:
            raise ValueError(f"missing real positive account ID for {key}")
        profiles[key] = transport.profile(uid, role_sets[key])
    report = {"phase": phase, "suite_sha256": suite_hash, "evaluator_sha256": evaluator_hashes,
              "service_version": initial_version,
              "excluded_cases": [{"id": case["id"], "reason": "known model vision limitation"}
                                 for case in excluded],
              "include_vision": include_vision,
              "identities": profiles, "planned": len(cases), "cases": [], "requests": [],
              "automatic_retries": 0, "business_write_approval": False,
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
        report["metrics"] = request_metrics.collect([r["trace"] for r in transport.requests])
        target = folder / "report.json"
        temp = target.with_suffix(".tmp")
        temp.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
        temp.replace(target)

    save()
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
        try:
            key = case["identity"]
            with transport.conversation(identities[key], role_sets[key]):
                if case.get("requires_quota_usage"):
                    transport.check_quota_fixture(case["requires_quota_usage"])
                if case.get("requires_category_creation"):
                    transport.check_category_fixture(case["requires_category_creation"])
                result = golden.run_case(case, turn_runner=transport)
                fails = golden.check_case(case, result)
                errors = [r["error"] for r in result["rounds"] if r.get("error")]
                row.update(status="failed" if fails or errors else "passed",
                           fails=fails, errors=errors, result=safe_result(result))
        except (httpx.HTTPError, EvaluationError, ValueError) as exc:
            # Evaluation failures stay visible and never become model-quality passes.
            row.update(status="evaluation_error", error=f"{type(exc).__name__}: {exc}")
        row["elapsed_s"] = round(time.monotonic() - started, 3)
        report["cases"].append(row)
        save()
        print(json.dumps({k: row[k] for k in ("id", "status", "elapsed_s")},
                         ensure_ascii=False), flush=True)
        if phase == "poc" and row["status"] == "evaluation_error":
            report["stopped_reason"] = "HTTP/evidence precondition failed"
            save()
            break
    report["finished_at"] = datetime.now().astimezone().isoformat()
    report["full_run"] = len(report["cases"]) == len(cases) and phase == "full"
    save()
    return report


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
    args = parser.parse_args()
    cases = load_suite()
    if args.phase == "poc":
        by_id = {c["id"]: c for c in cases}
        cases = [by_id[key] for key in POC_IDS]
    transport = OnlineTransport(agent_url=args.agent_url, blog_url=args.blog_url,
                                report_dir=args.report_dir, host=args.host)
    try:
        report = run(cases, transport,
                     {"admin": args.admin_uid, "user": args.user_uid, "zako": args.zako_uid},
                     args.report_dir, phase=args.phase,
                     suite_hash=hashlib.sha256(SUITE.read_bytes()).hexdigest(),
                     include_vision=args.include_vision)
    finally:
        transport.client.close()
    complete = len(report["cases"]) == report["planned"]
    return 0 if complete and all(c["status"] == "passed" for c in report["cases"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
