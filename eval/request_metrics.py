"""Request-level summaries independent of planner nodes and answer wording."""

import math
from collections import Counter, defaultdict
from statistics import median, quantiles

from eval import action_metrics, usage
from utils.trace_io import load_trace


def _mapping(value):
    return value if isinstance(value, dict) else {}


def _label(value):
    return value if isinstance(value, str) and value else "unknown"


def _event_duration(events, name):
    for event in events:
        if event.get("node") != "producer" or event.get("event") != name:
            continue
        value = event.get("duration_s")
        if type(value) in (int, float) and math.isfinite(value) and value >= 0:
            return value
    return None


def events_of(doc):
    events = doc.get("events")
    return [e for e in events if isinstance(e, dict)] if isinstance(events, list) else []


def path_of(doc):
    events = events_of(doc)
    runtime = _mapping(_mapping(doc.get("input")).get("runtime"))
    if any(e.get("event") == "legacy_handoff" for e in events):
        return "hybrid_to_graph"
    if runtime.get("name") == "native":
        return "native"
    if runtime.get("arm") in ("graph", "hybrid", "native"):
        return runtime["arm"]
    nodes = [_label(e.get("node")) for e in events]
    if "hybrid" in nodes:
        return "hybrid"
    return "graph" if "planner" in nodes else "unknown"


def summary(doc):
    meta = _mapping(doc.get("input"))
    runtime = _mapping(meta.get("runtime"))
    events = events_of(doc)
    duration = doc.get("duration_s")
    valid_duration = (type(duration) in (int, float) and math.isfinite(duration)
                      and duration >= 0)
    return {"source": _label(meta.get("source") or ("evaluation" if meta.get("golden") else None)),
            "build": _label(runtime.get("build_sha")),
            "model": _label(runtime.get("model")), "path": path_of(doc),
            "duration_s": duration if valid_duration else None,
            "first_visible_event_s": _event_duration(events, "first_visible_event"),
            "first_reply_s": _event_duration(events, "first_reply"),
            "has_reply": isinstance(doc.get("reply"), str) and bool(doc["reply"].strip()),
            "end_reason": _label(doc.get("end_reason")),
            "started_calls": sum(e.get("event") == "llm_start" for e in events),
            "errored_calls": sum(e.get("event") == "llm_error" for e in events),
            "handoff_reasons": [_label(e.get("reason")) for e in events
                                if e.get("event") == "legacy_handoff"],
            "budget_stop_reasons": [_label(e.get("reason")) for e in events
                                    if e.get("event") == "budget_stop"],
            "finalization_reasons": [_label(e.get("reason")) for e in events
                                      if e.get("event") == "finalizing"],
            **action_metrics.timings(events), **usage.totals(events)}


def collect(paths):
    groups = defaultdict(list)
    unreadable = 0
    for path in paths:
        doc = load_trace(path)
        if not isinstance(doc, dict):
            unreadable += 1
            continue
        row = summary(doc)
        groups[(row["source"], row["build"], row["model"], row["path"])].append(row)
    output = []
    for key, rows in sorted(groups.items()):
        times = [row["duration_s"] for row in rows if row["duration_s"] is not None]
        first_visible = [row["first_visible_event_s"] for row in rows
                         if row["first_visible_event_s"] is not None]
        first_reply = [row["first_reply_s"] for row in rows
                       if row["first_reply_s"] is not None]
        totals = usage.empty()
        for row in rows:
            for field in totals:
                totals[field] += row[field]
        output.append({"source": key[0], "build": key[1], "model": key[2], "path": key[3],
                       "requests": len(rows), "with_reply": sum(r["has_reply"] for r in rows),
                       "end_reasons": dict(Counter(r["end_reason"] for r in rows)),
                       "started_calls": sum(r["started_calls"] for r in rows),
                       "errored_calls": sum(r["errored_calls"] for r in rows),
                       "handoff_reasons": dict(Counter(reason for r in rows
                                                        for reason in r["handoff_reasons"])),
                       "budget_stops": sum(bool(r["budget_stop_reasons"]) for r in rows),
                       "budget_stop_reasons": dict(Counter(
                           reason for r in rows for reason in r["budget_stop_reasons"])),
                       "finalization_reasons": dict(Counter(
                           reason for r in rows for reason in r["finalization_reasons"])),
                       "duration_samples": len(times), "p50_s": median(times) if times else None,
                       "p95_s": quantiles(times, n=100, method="inclusive")[94]
                       if len(times) >= 20 else None,
                       "first_visible_samples": len(first_visible),
                       "first_visible_p50_s": median(first_visible) if first_visible else None,
                       "first_visible_p95_s": quantiles(
                           first_visible, n=100, method="inclusive")[94]
                       if len(first_visible) >= 20 else None,
                       "first_reply_samples": len(first_reply),
                       "first_reply_p50_s": median(first_reply) if first_reply else None,
                       "first_reply_p95_s": quantiles(first_reply, n=100, method="inclusive")[94]
                       if len(first_reply) >= 20 else None,
                       "measured_uncached_input": totals["cache_in"] - totals["cache"]
                       if totals["cache_seen"] else None,
                       "cache_hit_rate": usage.hit_rate(totals), **totals})
    return {"unreadable": unreadable, "groups": output}
