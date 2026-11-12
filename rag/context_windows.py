"""Select bounded source context around retrieved ranges without changing their ranking."""

from __future__ import annotations

from functools import lru_cache

from rag.sections import body_blocks

POLICY_VERSION = "source-block-context-v1"
MAX_READ_CHARS = 24000


@lru_cache(maxsize=2048)
def _blocks(text):
    return body_blocks(text)


def context_range(text, start, end, profile, *, mode="parent", max_tokens=1024):
    """Expand the anchor to parent/adjacent blocks; only margins may shrink to fit."""
    if (mode not in {"parent", "neighbors"} or type(max_tokens) is not int or max_tokens < 1
            or type(start) is not int or type(end) is not int or not 0 <= start < end <= len(text)):
        raise ValueError("valid source anchor, mode and positive context budget required")
    if profile.count(text[start:end]) > max_tokens or end - start > MAX_READ_CHARS:
        raise ValueError("context budget cannot contain the retrieved anchor")
    blocks = _blocks(text)
    first = next(i for i, (low, high) in enumerate(blocks) if low <= start < high)
    last = next(i for i, (low, high) in enumerate(blocks) if low < end <= high)
    if mode == "neighbors":
        first, last = max(0, first - 1), min(len(blocks) - 1, last + 1)
    left, right = start - blocks[first][0], blocks[last][1] - end

    def bounds(scale):
        return start - left * scale // 1024, end + right * scale // 1024

    a, b = bounds(1024)
    if b - a <= MAX_READ_CHARS and profile.count(text[a:b]) <= max_tokens:
        return {"offset": a, "end": b, "policy": mode, "policy_version": POLICY_VERSION,
                "budget_tokens": max_tokens, "bounded": False}
    low, high = 0, 1023
    while low < high:
        middle = (low + high + 1) // 2
        a, b = bounds(middle)
        if b - a <= MAX_READ_CHARS and profile.count(text[a:b]) <= max_tokens:
            low = middle
        else:
            high = middle - 1
    a, b = bounds(low)
    return {"offset": a, "end": b, "policy": mode, "policy_version": POLICY_VERSION,
            "budget_tokens": max_tokens, "bounded": low < 1024}


def expand_context(hits, section_text, profile, *, mode="parent", max_tokens=1024):
    """Keep leaf locators/excerpts intact; attach a separate read-window locator."""
    result = []
    for hit in hits:
        matches = []
        for match in hit.get("matches", []):
            text = section_text(hit, match["section_id"])
            budget = max(max_tokens, profile.count(text[match["offset"]:match["end"]]))
            window = context_range(text, match["offset"], match["end"], profile,
                                   mode=mode, max_tokens=budget)
            window["requested_budget_tokens"] = max_tokens
            matches.append({**match, "read_window": window})
        result.append({**hit, "matches": matches})
    return result


def merge_read_windows(matches):
    """Merge overlapping source requests while retaining first-hit priority and API bounds."""
    groups = {}
    for order, match in enumerate(matches):
        window = match.get("read_window", match)
        groups.setdefault(match["section_id"], []).append(
            (window["offset"], window["end"], order))
    result = []
    for section, spans in groups.items():
        merged = []
        for low, high, order in sorted(spans):
            if (merged and low <= merged[-1][1]
                    and max(high, merged[-1][1]) - merged[-1][0] <= MAX_READ_CHARS):
                a, b, previous = merged[-1]
                merged[-1] = a, max(b, high), min(previous, order)
            else:
                merged.append((low, high, order))
        result.extend({"section_id": section, "offset": a, "end": b, "priority": order}
                      for a, b, order in merged)
    return sorted(result, key=lambda row: row["priority"])
