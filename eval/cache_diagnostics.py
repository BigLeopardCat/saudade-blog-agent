"""Explain observed request changes without inferring a provider's cache key or TTL."""

import argparse
import json
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from eval import usage
from eval.token_cost_report import estimate_cost, parse_prices
from utils.trace_files import iter_trace_files
from utils.trace_io import load_trace


def common_prefix(left, right):
    return next((index for index, pair in enumerate(zip(left, right))
                 if pair[0] != pair[1]), min(len(left), len(right)))


def compare(previous, current):
    """Compare observed layouts; an identical payload can still miss provider cache."""
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
    layout, source_kind, flags = None, "unavailable", set()
    stamp = _timestamp(doc)
    for index, event in enumerate(doc.get("events", [])):
        kind = event.get("event")
        if kind in ("task_resumed", "confirmation_resumed", "compacted"):
            flags.add(kind)
        if kind in ("request_layout", "wire_request"):
            layout, source_kind = event, "wire" if kind == "wire_request" else "sdk"
        elif kind == "wire_request_unavailable":
            layout, source_kind = {}, "unavailable"
        elif kind in ("llm_done", "llm_error") and layout is not None:
            yield {"trace": source, "index": index, "layout": layout, "usage": event,
                   "layout_source": source_kind,
                   "phase": event.get("node"), "flags": sorted(flags),
                   "timestamp": stamp + event.get("t", 0) if stamp is not None else None}
            layout = None


def _summary_attempted(events):
    return any(event.get("node") == "summarizer"
               and event.get("event") in {"llm_start", "llm_done", "llm_error"}
               for event in events)


def _source_labels(doc):
    meta = doc.get("input")
    meta = meta if isinstance(meta, dict) else {}
    runtime = meta.get("runtime")
    runtime = runtime if isinstance(runtime, dict) else {}
    return tuple(value if isinstance(value, str) and value else "unknown"
                 for value in (meta.get("source"), runtime.get("build_sha")))


def request_cohorts(documents, prices=None):
    """Compare request lifecycles; first observed never implies a provider cold cache."""
    seen, after_compaction, groups = set(), set(), {}
    ordered = sorted(documents, key=lambda item: (
        _timestamp(item[1]) is None, _timestamp(item[1]) or 0, item[0]))
    for _, doc in ordered:
        events = [event for event in doc.get("events", []) if isinstance(event, dict)]
        layouts = [row["layout"] for row in _calls(doc, "")]
        layout = next((event for event in layouts if event.get("tool_choice") == "auto"),
                      layouts[0] if layouts else {})
        identity = (layout.get("endpoint"), layout.get("session"))
        comparable = all(identity) and all(layouts) and _timestamp(doc) is not None
        compacted = any(event.get("event") == "compacted" for event in events)
        if compacted or _summary_attempted(events):
            cohort = "compaction_request"
        elif not comparable:
            cohort = "unclassified"
        elif identity in after_compaction:
            cohort = "first_request_after_compaction"
        elif identity in seen:
            cohort = "continued_request"
        else:
            cohort = "first_observed_request"
        if comparable:
            seen.add(identity)
            after_compaction.discard(identity)
            if compacted:
                after_compaction.add(identity)
        models = {event["model"] for event in events
                  if event.get("event") in {"request_layout", "wire_request"}
                  and event.get("model")}
        model = next(iter(models)) if len(models) == 1 else "mixed_or_unknown"
        key = (cohort, model, *_source_labels(doc))
        if key not in groups:
            groups[key] = {"cohort": cohort, "model": model, "source": key[2],
                           "build": key[3], "requests": 0, **usage.empty()}
        row = groups[key]
        row["requests"] += 1
        for field, value in usage.totals(events).items():
            row[field] += value
    return [{**row, "hit_rate": usage.hit_rate(row),
             **usage.provider_costs(row),
             "measured_uncached_input": row["cache_in"] - row["cache"],
             "estimated_cost": estimate_cost(row, prices)
             if row["model"] != "mixed_or_unknown" else None}
            for _, row in sorted(groups.items())]


def diagnose(documents, *, prices=None):
    """Group by observed model/endpoint/session and keep summarizers separate."""
    documents = list(documents)
    calls = [row for source, doc in documents for row in _calls(doc, source)]
    calls.sort(key=lambda row: (row["timestamp"] is None, row["timestamp"] or 0,
                                row["trace"], row["index"]))
    previous, groups, result = {}, defaultdict(usage.empty), []
    for row in calls:
        layout = row["layout"]
        complete_identity = bool(layout.get("session") and layout.get("endpoint")
                                 and row["timestamp"] is not None)
        key = (layout.get("model"), layout.get("endpoint"), layout.get("session"),
               row["phase"], row["layout_source"],
               None if complete_identity else row["trace"])
        delta = compare(previous.get(key), layout) if layout else {"change": "unobserved"}
        previous[key] = layout
        event = row["usage"]
        measured = usage.totals([event])
        for field, value in measured.items():
            groups[delta["change"]][field] += value
        incoming, cached = usage.count(event.get("input")), usage.count(event.get("cache_read"))
        known = incoming is not None and cached is not None and cached <= incoming
        result.append({key: row[key] for key in ("trace", "index", "phase", "flags", "layout_source")} | delta | {
            "cross_request_comparable": complete_identity,
            "input": incoming, "cache_read": cached if known else None,
            "uncached_input": incoming - cached if known else None,
            "cache_observation": ("reported_hit" if cached else "reported_miss")
            if known else "unknown"})
    return {"calls": result, "groups": {name: dict(row, hit_rate=usage.hit_rate(row))
                                       for name, row in groups.items()},
            "request_cohorts": request_cohorts(documents, prices)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dir", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--price-in", type=float, help="Uncached input rate per million tokens")
    parser.add_argument("--price-out", type=float, help="Output rate per million tokens")
    parser.add_argument("--price-cache", type=float, help="Cached input rate per million tokens")
    parser.add_argument("--currency", type=str.upper,
                        help="Currency of the supplied rates, e.g. USD")
    args = parser.parse_args()
    try:
        prices = parse_prices(args)
    except ValueError as error:
        parser.error(str(error))
    if args.currency and not (len(args.currency) == 3 and args.currency.isascii()
                              and args.currency.isalpha()):
        parser.error("Currency must be a three-letter code")
    documents = [(str(Path(path).relative_to(args.dir)), doc)
                 for path in iter_trace_files(args.dir)
                 if isinstance(doc := load_trace(path), dict)]
    report = diagnose(documents, prices=prices)
    report["pricing"] = {"rates_per_million_tokens": dict(zip(
        ("uncached_input", "output_including_reasoning", "cached_input"), prices, strict=True))
        if prices is not None else None, "currency": args.currency,
                         "basis": "caller_supplied_estimate_not_provider_invoice"}
    Path(args.out).write_text(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
