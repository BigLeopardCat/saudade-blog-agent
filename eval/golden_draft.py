"""Collect private production evidence for review before task enrollment.

Drafts are never executable tests. Anonymize them, restore missing preconditions
and define expected outcomes before adding cases to native_requests.jsonl.
"""

import argparse
import difflib
import json
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from random import SystemRandom

sys.path.insert(0, str(Path(__file__).resolve().parent))
import trace_alert as ta  # noqa: E402

from utils.trace_files import iter_trace_files, parse_trace_name  # noqa: E402
from utils.trace_io import load_trace  # noqa: E402

GOLDEN = Path(__file__).resolve().parent / "golden" / "native_requests.jsonl"
OUT_DIR = Path("eval/report")
PAGE_FIELDS = ("current_url", "page_title", "current_effects", "current_darkmode")


def build_record(path, doc, stamp, uid):
    meta, events = doc.get("input") or {}, doc.get("events") or []
    page = meta.get("page_context") or {}
    context = {key: page[key] for key in PAGE_FIELDS if isinstance(page, dict) and key in page}
    role = meta.get("principal_role")
    identity = ("admin" if role in {"admin", "superadmin"}
                else role if role in {"user", "zako"} else None)
    if meta.get("principal_source") != "assertion":
        identity = None
    calls = [e for e in events if e.get("node") == "native" and e.get("event") == "call"]
    missing = []
    if len(context) != len(PAGE_FIELDS):
        missing.append("page_context")
    message = str(meta.get("message") or "")
    if meta.get("message_truncated", len(message) >= 200):
        missing.append("complete_user_request")
    if identity is None:
        missing.append("identity")
    if any(e.get("kind") == "prepared" for e in calls):
        missing.append("confirmation_preview_and_signed_intent")
    return {"trace_id": doc.get("trace_id"), "path": str(path), "stamp": stamp, "uid": uid,
            "conversation_id": meta.get("conversation_id"), "identity": identity,
            "principal_role": role, "user_input": message, "reply": doc.get("reply") or "",
            "context": context, "tools": [e.get("name") for e in calls],
            "missing_evidence": missing}


def signals(doc):
    meta, events = doc.get("input") or {}, doc.get("events") or []
    result = []
    if ta.USER_CHALLENGE.search(str(meta.get("message") or "")):
        result.append("user_feedback")
    if ta.AGENT_SELF_DENY.search(str(doc.get("reply") or "")):
        result.append("assistant_self_denial")
    if any(e.get("event") in {"tool_failed", "budget_stop", "llm_error"}
           or e.get("kind") in {"invalid_args", "error"} for e in events):
        result.append("runtime_or_tool_issue")
    return result


def collect(paths, since, until, excluded=()):
    history, candidates, ordinary = defaultdict(list), [], []
    scanned = 0
    for path in sorted(paths):
        stamp, uid = parse_trace_name(str(path))
        if not stamp or not since <= stamp <= until:
            continue
        doc = load_trace(str(path))
        if not doc or int(uid) == 0 or doc.get("trace_id") in excluded:
            continue
        meta = doc.get("input") or {}
        if meta.get("golden") or meta.get("source") == "evaluation":
            continue
        scanned += 1
        if not meta.get("message"):
            continue
        record = build_record(path, doc, stamp, int(uid))
        key = (uid, record["principal_role"], record["conversation_id"] or record["trace_id"])
        prior = history[key]
        record["prior_turns"] = [{k: turn[k] for k in ("trace_id", "user_input", "reply", "path")}
                                 for turn in prior[-2:]]
        record["signals"] = signals(doc)
        (candidates if record["signals"] else ordinary).append(record)
        prior.append(record)
    return candidates, ordinary, scanned


def load_golden_inputs(path=GOLDEN):
    inputs = {}
    for line in Path(path).read_text().splitlines():
        if not line.strip():
            continue
        case = json.loads(line)
        for index, turn in enumerate(case.get("rounds", [case])):
            inputs[f"{case['id']}:{index + 1}"] = turn["user_input"]
    return inputs


def draft(record, index, inputs):
    matches = [(difflib.SequenceMatcher(None, record["user_input"], text).ratio(), key)
               for key, text in inputs.items()]
    ratio, key = max(matches, default=(0, ""))
    return {"id": f"draft_{record['stamp']}_{index}", "identity": record["identity"],
            "tags": ["draft"], "user_input": record["user_input"], "context": record["context"],
            "gold": {}, "_draft": {"evidence": record, "nearest_case": [key, round(ratio, 2)],
                "review_required": ["anonymize", "restore_preconditions", "define_expected_outcome",
                                    "define_semantic_rubric",
                                    "state_and_browser_checks_if_needed"]}}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=1)
    parser.add_argument("--from", dest="since", default="")
    parser.add_argument("--to", dest="until", default="99999999")
    parser.add_argument("--limit", type=int, default=20,
                        help="Maximum flagged candidates; 0 means all")
    parser.add_argument("--sample", type=int, default=5,
                        help="Additional ordinary traces to review")
    parser.add_argument("--exclude-report", action="append", default=[],
                        help="Exclude evaluation request IDs")
    args = parser.parse_args()
    if args.days < 1 or args.limit < 0 or args.sample < 0:
        parser.error("days must be positive; limit and sample must be nonnegative")
    excluded = set()
    for path in args.exclude_report:
        report = json.loads(Path(path).read_text())
        excluded.update(row["request_id"] for row in report.get("requests", []))
    since = args.since or (datetime.now() - timedelta(days=args.days)).strftime("%Y%m%d")
    flagged, ordinary, scanned = collect(iter_trace_files(ta.TRACE_DIR), since,
                                         args.until, excluded)
    candidates = list(reversed(flagged))[:args.limit or None]
    for record in SystemRandom().sample(ordinary, min(args.sample, len(ordinary))):
        candidates.append({**record, "signals": ["ordinary_sample_not_a_failure"]})
    inputs = load_golden_inputs()
    drafts = [draft(record, index, inputs) for index, record in enumerate(candidates)]
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    name = OUT_DIR / ("golden_drafts_" + datetime.now().strftime("%Y%m%d_%H%M%S"))
    with name.with_suffix(".jsonl").open("x") as file:
        for item in drafts:
            file.write(json.dumps(item, ensure_ascii=False) + "\n")
    with name.with_suffix(".md").open("x") as file:
        file.write("# 当前任务集候选人审单\n\n草稿含私有现场，只在本地保存，不自动加入用例库。\n\n")
        file.write("补齐缺失证据、匿名化和可复位前提，定义最终结果与语义判据；"
                   "检查 native_requests.jsonl 是否已有等价场景。\n\n")
        for item in drafts:
            evidence = item["_draft"]["evidence"]
            file.write(f"## {item['id']}\n\n信号：{evidence['signals']}\n\n"
                       f"缺失证据：{evidence['missing_evidence']}\n\n来源：{evidence['path']}\n\n"
                       f"用户：{item['user_input']}\n\n回复：{evidence['reply']}\n\n"
                       "- [ ] 确认问题与正确结果\n- [ ] 完成匿名化、前提与判据\n"
                       "- [ ] 在独立夹具复现并加入当前任务集\n\n")
    print(json.dumps({"scanned": scanned, "flagged": len(flagged), "drafts": len(drafts),
                      "suite": str(GOLDEN), "output": str(name)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
