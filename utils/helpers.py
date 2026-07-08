"""Miscellaneous helper utilities."""

from datetime import datetime


def format_message(role: str, content: str) -> str:
    """Return a human-readable formatted chat message string."""
    timestamp = datetime.now().strftime("%H:%M:%S")
    return f"[{timestamp}] {role.upper()}: {content}"
