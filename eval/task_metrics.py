"""Logical task accounting across HTTP attempts; execution phase is not task quality."""

import argparse
import json
import math
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

from eval import request_metrics, usage
from utils.trace_io import load_trace

_COUNTERS = ("tokens", "model_calls", "tool_calls")


def _spent(value):
    if (isinstance(value, dict) and all(usage.count(value.get(key)) is not None
                                       for key in _COUNTERS)):
        return {key: value[key] for key in _COUNTERS}
    return None


def _started(doc):
    try:
        value = datetime.fromisoformat(doc["started_at"])
        normalized = value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value
        stamp = normalized.timestamp()
        return stamp if math.isfinite(stamp) else None
    except (KeyError, TypeError, ValueError, OverflowError):
        return None


def _clock(doc):
    try:
        return "aware" if datetime.fromisoformat(doc["started_at"]).tzinfo else "naive"
    except (KeyError, TypeError, ValueError):
        return "unknown"


def _phase(doc, events):
    phases = [event.get("phase") for event in events
              if event.get("node") == "native" and event.get("event") == "turn_state"]
    if phases:
        return phases[-1] if isinstance(phases[-1], str) else "unknown"
    if doc.get("end_reason") in ("cancelled", "client_closed", "total_timeout", "idle_timeout"):
        return "interrupted"
    return "unknown"


def _identity(doc, events):
    bindings = [event for event in events
                if event.get("node") == "budget" and event.get("event") == "task_restored"]
    ids = {event.get("task_id") for event in bindings
           if isinstance(event.get("task_id"), str) and event["task_id"]}
    meta = doc.get("input")
    meta = meta if isinstance(meta, dict) else {}
    uid, cid = doc.get("user_id"), meta.get("conversation_id")
    if (len(ids) != 1 or type(uid) is not int or uid <= 0
            or type(cid) is not int or cid <= 0 or meta.get("principal_source") != "assertion"):
        return None
    source = meta.get("source") or "unknown"
    role = meta.get("principal_role")
    if not isinstance(source, str) or not isinstance(role, str) or not role:
        return None
    return source, uid, role, cid, ids.pop()


def _task(key, attempts):
    attempts.sort(key=lambda row: (row["start"] is None, row["start"] or 0, row["trace_id"]))
    totals = usage.empty()
    snapshots, previous, complete = [], None, True
    for row in attempts:
        for field in totals:
            totals[field] += row["usage"][field]
        restored, snapshot = row["restored"], row["snapshot"]
        before, after = _spent(restored), _spent((snapshot or {}).get("spent"))
        increment = _spent((snapshot or {}).get("request_spent"))
        consistent = bool(before is not None and after is not None and increment is not None
                          and all(after[k] == before[k] + increment[k] for k in _COUNTERS))
        complete &= bool(consistent and before == (previous or dict.fromkeys(_COUNTERS, 0)))
        previous = after
        snapshots.append({"trace_id": row["trace_id"], "before": before, "after": after,
                          "request_spent": increment, "consistent": consistent})
    starts = [row["start"] for row in attempts if row["start"] is not None]
    clocks = {row["clock"] for row in attempts}
    durations = [row["duration"] for row in attempts if row["duration"] is not None]
    latest = attempts[-1]
    # Cumulative reservations are a separate accounting basis, never added to
    # provider usage or labelled as provider charges.
    return {"source": key[0], "user_id": key[1], "role": key[2], "conversation_id": key[3],
            "task_id": key[4], "requests": len(attempts),
            "trace_ids": [row["trace_id"] for row in attempts],
            "builds": sorted({row["build"] for row in attempts}),
            "models": sorted({row["model"] for row in attempts}),
            "last_observed_phase": latest["phase"],
            "phase_counts": dict(Counter(r["phase"] for r in attempts)),
            "active_request_s": sum(durations) if len(durations) == len(attempts) else None,
            "elapsed_s": (max(row["start"] + row["duration"] for row in attempts) - min(starts)
                          if len(starts) == len(durations) == len(attempts) and len(clocks) == 1
                          else None),
            "budget_history_complete": complete, "budget_snapshots": snapshots,
            "budget_spent": _spent(latest["snapshot"].get("spent")) if latest["snapshot"] else None,
            "observed_usage_complete": usage.complete(totals),
            "provider_usage_complete": bool(complete and previous is not None
                and previous["model_calls"] == totals["calls"]
                and (usage.complete(totals) or previous["model_calls"] == 0
                     and totals["unknown_attempts"] == 0)),
            "cache_hit_rate": usage.hit_rate(totals), **totals}


def collect(paths):
    documents, conflicts = {}, set()
    unreadable = duplicates = 0
    for path in paths:
        doc = load_trace(str(path))
        if not doc:
            unreadable += 1
            continue
        identity = doc.get("trace_id")
        if not isinstance(identity, str) or not identity:
            identity = str(Path(path).resolve())
        if identity in documents:
            if documents[identity] == doc:
                duplicates += 1
            else:
                conflicts.add(identity)
        else:
            documents[identity] = doc
    groups, unassigned = defaultdict(list), 0
    for identity, doc in documents.items():
        if identity in conflicts:
            continue
        events = request_metrics.events_of(doc)
        key = _identity(doc, events)
        if key is None:
            unassigned += 1
            continue
        restored = next(event for event in events
                        if event.get("node") == "budget" and event.get("event") == "task_restored")
        snapshots = [event for event in events if event.get("node") == "budget"
                     and event.get("event") == "task_usage" and event.get("task_id") == key[-1]]
        row = request_metrics.summary(doc)
        groups[key].append({"trace_id": identity, "start": _started(doc),
                            "duration": row["duration_s"], "phase": _phase(doc, events),
                            "clock": _clock(doc),
                            "restored": restored, "snapshot": snapshots[-1] if snapshots else None,
                            "usage": usage.totals(events), "build": row["build"],
                            "model": row["model"]})
    tasks = [_task(key, rows) for key, rows in sorted(groups.items())]
    return {"tasks": tasks, "unreadable": unreadable, "duplicate_traces": duplicates,
            "conflicting_trace_ids": sorted(conflicts), "unassigned_requests": unassigned,
            "coverage": {"tasks": len(tasks), "requests": sum(t["requests"] for t in tasks),
                         "complete_budget_histories": sum(
                             t["budget_history_complete"] for t in tasks),
                         "complete_provider_usage": sum(
                             t["provider_usage_complete"] for t in tasks)},
            "limitations": ["Latest phase describes captured traces, "
                            "not current task state or success.",
                            "Untraced controls (including button cancellation) "
                            "do not update this phase.",
                            "Elapsed includes human/browser waiting; "
                            "active_request_s sums request durations.",
                            "Budget tokens include conservative reservations, "
                            "not provider billing.",
                            "Missing earlier attempts or final snapshots "
                            "make budget history incomplete.",
                            "Cache ratio covers only responses with valid cache measurements.",
                            "Naive timestamps use a shared local clock; "
                            "mixed clocks omit elapsed."]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dir", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    from utils.trace_files import iter_trace_files
    report = collect(iter_trace_files(str(args.dir)))
    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(report["coverage"], ensure_ascii=False))


if __name__ == "__main__":
    main()
