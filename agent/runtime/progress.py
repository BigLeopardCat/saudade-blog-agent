"""Bounded native progress and timing on the existing SSE transport."""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Callable

from agent.runtime.control import AgentCancelled, RequestControl
from utils.trace import record

_MAX_EVENTS = 24
# Match the frontend's hidden command prefixes, not user intent.
_HIDDEN_PREFIX = re.compile(
    r"^\s*(?:[A-Za-z0-9_]*EFFECT|DARKMODE|NAVIGATE|AUTO_NAVIGATE|SUMMARY|\[?System)\]?\s*:"
)


def visible_reply(text: str) -> bool:
    return bool(text.strip()) and not _HIDDEN_PREFIX.match(text)


class ProgressEmitter:
    """Deduplicate static labels within one sequential tool loop."""

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
        return self.sink(text) is not False


class StreamProgress:
    """Schedule nonblocking progress and measure server emission, not UI paint."""

    def __init__(self, loop: asyncio.AbstractEventLoop, queue: asyncio.Queue,
                 control: RequestControl):
        self.loop, self.queue, self.control = loop, queue, control
        self.started = time.monotonic()
        self.emitter = ProgressEmitter(self.enqueue)
        self._visible = False
        self._reply = False

    def _active(self) -> bool:
        try:
            self.control.check()
        except (AgentCancelled, TimeoutError):
            return False
        return not self.loop.is_closed()

    def enqueue(self, text: str) -> bool:
        if not self._active():
            return False
        try:
            self.loop.call_soon_threadsafe(self._deliver, text)
        except RuntimeError:
            # The loop can close between the check and scheduling.
            return False
        return True

    def _deliver(self, text: str):
        if self._active():
            self.queue.put_nowait(f"__PROCESS__:{text}")

    def reply(self, text: str) -> bool:
        if not visible_reply(text):
            return False
        self.mark("reply")
        return True

    def mark(self, kind: str):
        elapsed = time.monotonic() - self.started
        if not self._visible:
            self._visible = True
            record("producer", "first_visible_event", kind=kind, duration_s=elapsed)
        if kind == "reply" and not self._reply:
            self._reply = True
            record("producer", "first_reply", duration_s=elapsed)


def emit(config: dict | None, text: str, *, key: str) -> bool:
    cfg = (config or {}).get("configurable") or {}
    current = cfg.get("progress_emitter")
    return current.emit(text, key=key) if isinstance(current, ProgressEmitter) else False
