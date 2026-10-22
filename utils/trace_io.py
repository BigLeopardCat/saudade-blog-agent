# -*- coding: utf-8 -*-
"""Shared JSON/gzip trace reader for online diagnostics and offline evaluation."""
import gzip
import json

# Retain the old event key during the 30-day trace retention window.
# Remove the alias after 2026-11-16, once all retained traces use workflow.
LEGACY_EVENT_KEY = "skill"
LEGACY_SUNSET = "20261116"


def _alias_legacy_event_key(doc: dict) -> dict:
    """旧 trace 的事件键 `skill` 补一份 `workflow`（新键已在则不动，不覆盖新值）。"""
    for ev in doc.get("events") or []:
        if isinstance(ev, dict) and LEGACY_EVENT_KEY in ev and "workflow" not in ev:
            ev["workflow"] = ev[LEGACY_EVENT_KEY]
    return doc


def load_trace(path: str) -> dict | None:
    """读一份 trace（`.json` 与 `.json.gz` 都认）；坏文件返回 None，不抛。

    「读不到」与「里面没有那件事」是两件事：调用端一律按前者处理，不许把 None 当成
    "这份 trace 里没有我要找的东西"（扫描不能因为一个坏文件中断，也不能因此改口径）。
    """
    try:
        if path.endswith(".gz"):
            doc = json.loads(gzip.decompress(open(path, "rb").read()))
        else:
            doc = json.load(open(path))
    except Exception:
        return None
    if not isinstance(doc, dict):
        return None
    events = doc.get("events")
    if events is not None and (not isinstance(events, list)
                               or any(not isinstance(e, dict) for e in events)):
        return None
    return _alias_legacy_event_key(doc)
