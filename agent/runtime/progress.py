"""Small, bounded adapter for native runtime progress events.

Progress uses the existing ``__PROCESS__`` wire frame.  The runtime only knows
about safe, static labels; the transport callback decides how to enqueue them.
"""

from __future__ import annotations

from collections.abc import Callable

_MAX_EVENTS = 24


class ProgressEmitter:
    """Deduplicate progress labels for one request and forward them safely."""

    def __init__(self, sink: Callable[[str], object] | None = None):
        self.sink = sink
        self._seen: set[str] = set()
        self._count = 0
        self._last_text = ""

    def emit(self, text: str, *, key: str) -> bool:
        if not self.sink or not isinstance(text, str) or not text:
            return False
        if (key in self._seen or self._count >= _MAX_EVENTS
                or text == self._last_text):
            return False
        self._seen.add(key)
        self._count += 1
        self._last_text = text
        try:
            self.sink(text)
        except Exception:
            # Progress is observability, never a reason to fail the turn.
            return False
        return True


def emitter(config: dict | None) -> ProgressEmitter | None:
    cfg = (config or {}).get("configurable") or {}
    value = cfg.get("progress_emitter")
    return value if isinstance(value, ProgressEmitter) else None


def emit(config: dict | None, text: str, *, key: str) -> bool:
    current = emitter(config)
    return current.emit(text, key=key) if current else False
