# -*- coding: utf-8 -*-
"""Shared JSON/gzip trace reader for online diagnostics and offline evaluation."""
import gzip
import json
import zlib
from os import PathLike
from pathlib import Path

# Archived failed runs may outlive production retention; normalize at this read boundary only.
LEGACY_EVENT_KEY = "skill"


def _alias_legacy_event_key(doc: dict) -> dict:
    """旧 trace 的事件键 `skill` 补一份 `workflow`（新键已在则不动，不覆盖新值）。"""
    for ev in doc.get("events") or []:
        if isinstance(ev, dict) and LEGACY_EVENT_KEY in ev and "workflow" not in ev:
            ev["workflow"] = ev[LEGACY_EVENT_KEY]
    return doc


def load_trace(path: str | PathLike[str]) -> dict | None:
    """Read a UTF-8 JSON or gzip trace; unreadable data returns None, never an empty trace."""
    try:
        source = Path(path)
        reader = gzip.open if source.suffix == ".gz" else open
        with reader(source, "rt", encoding="utf-8") as stream:
            doc = json.load(stream)
    except (OSError, ValueError, UnicodeError, EOFError, zlib.error):
        return None
    if not isinstance(doc, dict):
        return None
    events = doc.get("events")
    if events is not None and (not isinstance(events, list)
                               or any(not isinstance(e, dict) for e in events)):
        return None
    return _alias_legacy_event_key(doc)
