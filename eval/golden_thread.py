"""Stable evaluation coordinates isolate cases, repetitions and reruns."""

import os
import re

LEGACY = "golden_thread"

ENV_REP = "GOLDEN_REP"

_PREFIX = "golden"
_UNSAFE = re.compile(r"[^0-9A-Za-z_]+")


def _slug(s: str) -> str:
    """Normalize coordinate segments to ASCII letters, digits and underscores.
    Empty segments become x so repeated requests use the same nonempty coordinate."""
    return _UNSAFE.sub("_", s).strip("_") or "x"


def rep() -> str:
    """Return the optional repetition identifier from GOLDEN_REP."""
    return (os.environ.get(ENV_REP) or "").strip()


def for_case(case_id: str, *, suffix: str = "") -> str:
    """Return the same coordinate for the same case, repetition and suffix."""
    parts = [_PREFIX, "native", _slug(case_id + suffix)]
    r = rep()
    if r:
        parts.append("r" + _slug(r))
    return "_".join(parts)
