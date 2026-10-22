"""Usage aggregation with explicit coverage; absent measurements are not zero."""


def empty():
    return dict(calls=0, measured=0, unnamed=0, output_seen=0,
                cache_seen=0, cache_in=0, cache=0, reasoning_seen=0,
                reasoning=0, **{"in": 0, "out": 0})


def count(value):
    return value if type(value) is int and value >= 0 else None


def add(row, event):
    row["calls"] += 1
    incoming = count(event.get("input"))
    outgoing = count(event.get("output"))
    cached = count(event.get("cache_read"))
    reasoning = count(event.get("reasoning"))
    if incoming is not None:
        row["measured"] += 1
        row["in"] += incoming
    if outgoing is not None:
        row["output_seen"] += 1
        row["out"] += outgoing
    if incoming is not None and cached is not None and cached <= incoming:
        row["cache_seen"] += 1
        row["cache_in"] += incoming
        row["cache"] += cached
    else:
        row["unnamed"] += 1
    if reasoning is not None and outgoing is not None and reasoning <= outgoing:
        row["reasoning_seen"] += 1
        row["reasoning"] += reasoning


def totals(events):
    row = empty()
    for event in events:
        if isinstance(event, dict) and event.get("event") == "llm_done":
            add(row, event)
    return row


def hit_rate(row):
    return row["cache"] / row["cache_in"] if row["cache_in"] else None


def complete(row):
    return row["calls"] > 0 and row["measured"] == row["output_seen"] == row[
        "cache_seen"] == row["calls"]
