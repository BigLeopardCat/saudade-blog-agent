"""Explain observed request changes without inferring a provider's cache key or TTL."""

import argparse
import json
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from eval import usage
from utils.trace_files import iter_trace_files
from utils.trace_io import load_trace


def common_prefix(left, right):
    return next((index for index, pair in enumerate(zip(left, right))
                 if pair[0] != pair[1]), min(len(left), len(right)))


def compare(previous, current):
    """Only compare recorded SDK layouts; an identical payload can still miss cache."""
    if previous is None:
        return {"change": "first_observed"}
    before, after = previous.get("messages", []), current.get("messages", [])
    shared = common_prefix(before, after)
    old_controls, new_controls = (previous.get("control_fingerprints"),
                                  current.get("control_fingerprints"))
    changed_keys = (sorted(key for key in old_controls.keys() | new_controls.keys()
                           if old_controls.get(key) != new_controls.get(key))
                    if old_controls is not None and new_controls is not None else None)
    # The complete control hash is authoritative; newly exposed summary fields
    # must not make unchanged older SDK payloads look like generation changes.
    keys = ["model", "endpoint", "session", "controls"]
    if previous.get("controls") is None or current.get("controls") is None:
        keys.extend(("tool_choice", "thinking", "reasoning_effort"))
    changed_controls = any(previous.get(key) != current.get(key) for key in keys)
    tools_changed = previous.get("tools") != current.get("tools")
    tools_before, tools_after = (previous.get("tool_fingerprints"),
                                 current.get("tool_fingerprints"))
    tool_index = (common_prefix(tools_before, tools_after)
                  if tools_before is not None and tools_after is not None else None)
    if changed_controls:
        change = "controls_changed"
    elif tools_changed:
        change = "tool_schema_changed"
    elif shared != len(before) or len(after) < len(before):
        change = "message_prefix_changed"
    else:
        change = "same_payload" if before == after else "append_only"
    return {"change": change, "common_messages": shared,
            "first_changed_message": shared if before != after else None,
            "first_changed_tool": tool_index if tools_changed else None,
            "tools_appended": bool(tools_changed and tool_index is not None
                                   and tool_index == len(tools_before)
                                   and len(tools_after) > len(tools_before)),
            "controls_changed": changed_controls, "changed_control_keys": changed_keys}


def _timestamp(doc):
    try:
        stamp = datetime.fromisoformat(doc.get("started_at", ""))
        return stamp.timestamp() if stamp.tzinfo is not None else None
    except (TypeError, ValueError):
        return None


def _calls(doc, source):
    layout, flags = None, set()
    stamp = _timestamp(doc)
    for index, event in enumerate(doc.get("events", [])):
        kind = event.get("event")
        if kind in ("task_resumed", "confirmation_resumed", "compacted"):
            flags.add(kind)
        if kind == "request_layout":
            layout = event
        elif kind in ("llm_done", "llm_error") and layout is not None:
            yield {"trace": source, "index": index, "layout": layout, "usage": event,
                   "phase": event.get("node"), "flags": sorted(flags),
                   "timestamp": stamp + event.get("t", 0) if stamp is not None else None}
            layout = None


def diagnose(documents):
    """Group by observed model/endpoint/session and keep summarizers separate."""
    calls = [row for source, doc in documents for row in _calls(doc, source)]
    calls.sort(key=lambda row: (row["timestamp"] is None, row["timestamp"] or 0,
                                row["trace"], row["index"]))
    previous, groups, result = {}, defaultdict(usage.empty), []
    for row in calls:
        layout = row["layout"]
        complete_identity = bool(layout.get("session") and layout.get("endpoint")
                                 and row["timestamp"] is not None)
        key = (layout.get("model"), layout.get("endpoint"), layout.get("session"),
               row["phase"], None if complete_identity else row["trace"])
        delta = compare(previous.get(key), layout)
        previous[key] = layout
        event = row["usage"]
        measured = usage.totals([event])
        for field, value in measured.items():
            groups[delta["change"]][field] += value
        incoming, cached = usage.count(event.get("input")), usage.count(event.get("cache_read"))
        known = incoming is not None and cached is not None and cached <= incoming
        result.append({key: row[key] for key in ("trace", "index", "phase", "flags")} | delta | {
            "cross_request_comparable": complete_identity,
            "input": incoming, "cache_read": cached if known else None,
            "uncached_input": incoming - cached if known else None,
            "cache_observation": ("reported_hit" if cached else "reported_miss")
            if known else "unknown"})
    return {"calls": result, "groups": {name: dict(row, hit_rate=usage.hit_rate(row))
                                       for name, row in groups.items()}}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dir", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    documents = [(str(Path(path).relative_to(args.dir)), doc)
                 for path in iter_trace_files(args.dir)
                 if isinstance(doc := load_trace(path), dict)]
    Path(args.out).write_text(json.dumps(diagnose(documents), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
