"""Positive signed-i32 record identifiers used by the Rust host."""


def normalize_target_id(value) -> int | None:
    """Read integer DTO fields without coercing bools, floats or display labels."""
    if type(value) is int:
        return value if 0 < value <= 2_147_483_647 else None
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value.isascii() or not value.isdecimal():
        return None
    value = value.lstrip("0")
    if not value or len(value) > 10:
        return None
    parsed = int(value)
    return parsed if parsed <= 2_147_483_647 else None
