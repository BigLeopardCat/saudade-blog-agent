# -*- coding: utf-8 -*-
"""Shared JSON/gzip trace reader for online diagnostics and offline evaluation."""
import gzip
import json
import zlib
from os import PathLike
from pathlib import Path


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
    return doc
