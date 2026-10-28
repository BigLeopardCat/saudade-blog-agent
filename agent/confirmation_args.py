"""Reject unresolved argument placeholders when signing concrete operations."""

import re

_REFERENCE = re.compile(r"^\$([a-z_][a-z0-9_]*)\[(\d+)\](?:\.([A-Za-z0-9_.]+))?$")


def _contains_reference(value) -> bool:
    if isinstance(value, str):
        return _REFERENCE.match(value.strip()) is not None
    if isinstance(value, dict):
        return any(_contains_reference(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return any(_contains_reference(item) for item in value)
    return False


def has_refs(specs) -> bool:
    """Signed operation arguments must contain values, including nested values."""
    return any(isinstance(spec, dict) and _contains_reference(spec.get("args"))
               for spec in specs or [])
