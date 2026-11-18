"""Shared JSON/gzip trace reader for online diagnostics and offline evaluation."""
import gzip
import json
import zlib
from contextlib import nullcontext
from os import PathLike
from pathlib import Path
from typing import BinaryIO


def read_trace(stream: BinaryIO, *, compressed: bool = False) -> dict | None:
    """Decode and validate a UTF-8 trace from the supplied capture."""
    try:
        reader = gzip.GzipFile(fileobj=stream, mode="rb") if compressed else nullcontext(stream)
        with reader as data:
            doc = json.loads(data.read().decode("utf-8"))
    except (OSError, ValueError, UnicodeError, EOFError, zlib.error):
        return None
    if not isinstance(doc, dict):
        return None
    events = doc.get("events")
    if events is not None and (not isinstance(events, list)
                               or any(not isinstance(e, dict) for e in events)):
        return None
    return doc


def load_trace(path: str | PathLike[str]) -> dict | None:
    """Read a JSON or gzip trace, returning None for an unreadable capture."""
    try:
        source = Path(path)
        with source.open("rb") as stream:
            return read_trace(stream, compressed=source.suffix == ".gz")
    except (OSError, ValueError):
        return None
