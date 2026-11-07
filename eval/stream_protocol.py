"""Validate application events decoded by httpx-sse for all online evaluators."""

import json

from agent.contracts.events import StreamEvent


class EventStreamError(ValueError):
    """A malformed or incomplete stream cannot count as a successful task."""


def read_events(source):
    done = failed = False
    for event in source.iter_sse():
        kind = event.event
        if done or (failed and kind != "done"):
            raise EventStreamError("unexpected event after terminal")
        try:
            data = json.loads(event.data)
        except (ValueError, TypeError) as exc:
            raise EventStreamError("invalid SSE JSON") from exc
        if kind in {"text", "error"}:
            valid = isinstance(data, str)
        elif kind in {"done", "text_reset"}:
            valid = type(data) is dict and not data
        else:
            try:
                StreamEvent(kind, data)
                valid = True
            except ValueError:
                valid = False
        if not valid:
            raise EventStreamError(f"invalid {kind} event payload")
        done = kind == "done"
        failed = failed or kind == "error"
        yield kind, data
    if not done and not failed:
        raise EventStreamError("HTTP stream ended without done")
