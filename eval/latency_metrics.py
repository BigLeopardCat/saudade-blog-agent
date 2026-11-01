"""Observed model spans and tool results; provider queue time remains unknown."""

import math
from collections import defaultdict
from statistics import median, quantiles


def seconds(value):
    return value if type(value) in (int, float) and math.isfinite(value) and value >= 0 else None


def distribution(values):
    values = [value for value in values if seconds(value) is not None]
    return {"samples": len(values), "p50_s": median(values) if values else None,
            "p95_s": quantiles(values, n=100, method="inclusive")[94]
            if len(values) >= 20 else None, "max_s": max(values) if values else None}


def model_calls(events):
    """Match serial model attempts in event order, retaining incomplete attempts."""
    rows = []
    for event in events:
        kind, node = event.get("event"), event.get("node")
        if kind not in {"llm_start", "llm_done", "llm_error", "model_first_token"}:
            continue
        if kind == "llm_start":
            phase = "summary" if node == "summarizer" else event.get("phase")
            rows.append(dict(node=node, round=event.get("round"),
                             phase=phase if isinstance(phase, str) and phase else "unknown",
                             status="unfinished", total_s=None, first_token_s=None,
                             remaining_s=None))
            continue
        row = next((row for row in reversed(rows) if row["status"] == "unfinished"
                    and row["node"] == node and row["round"] == event.get("round")), None)
        if row is None:
            continue
        duration = seconds(event.get("duration_s"))
        if kind == "model_first_token":
            if row["first_token_s"] is None:
                row["first_token_s"] = duration
        else:
            row.update(status="completed" if kind == "llm_done" else "error", total_s=duration)
            first = row["first_token_s"]
            if duration is not None and first is not None and first <= duration:
                row["remaining_s"] = duration - first
    return rows


def tool_calls(events):
    """Measure request-to-result spans, without summing potentially parallel work."""
    rows, active = [], {}
    for event in events:
        call = event.get("call_id")
        if event.get("node") != "native" or not isinstance(call, str):
            continue
        kind = event.get("event")
        if kind == "tool_requested":
            row = {"name": event.get("name"), "call_id": call,
                   "started_s": seconds(event.get("t")), "elapsed_s": None,
                   "status": "unfinished", "kind": "unknown"}
            rows.append(row)
            active[call] = row
        elif kind in {"call", "load_domain", "tool_error"} and call in active:
            row = active.pop(call)
            row.update(status="error" if kind == "tool_error" else "completed",
                       kind="discovery" if kind == "load_domain" else "business")
            ended = seconds(event.get("t"))
            if row["started_s"] is not None and ended is not None and ended >= row["started_s"]:
                row["elapsed_s"] = ended - row["started_s"]
    return rows


def aggregate(calls):
    phases = defaultdict(list)
    for call in calls:
        phases[call["phase"]].append(call)
    return {phase: {"attempts": len(rows),
                    "completed": sum(row["status"] == "completed" for row in rows),
                    "errors": sum(row["status"] == "error" for row in rows),
                    "unfinished": sum(row["status"] == "unfinished" for row in rows),
                    **{field: distribution([row[field] for row in rows])
                       for field in ("total_s", "first_token_s", "remaining_s")}}
            for phase, rows in sorted(phases.items())}


def request_phases(rows):
    return aggregate(call for row in rows for call in row["model_latency"])
