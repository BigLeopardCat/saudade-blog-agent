"""Trace explicitly selected, validated service facts within their tool invocation."""

import json
from contextlib import contextmanager
from contextvars import ContextVar

from utils.trace import record

_call_id = ContextVar("evidence_call_id", default=None)
MAX_FACT_CHARS = 8000


@contextmanager
def scope(call, round_index=None):
    token = _call_id.set(((call or {}).get("id"), round_index))
    try:
        yield
    finally:
        _call_id.reset(token)


def observed(source, value, fields):
    """Only existing-state fields are selected; proposed text is never evidence."""
    facts = {key: value[key] for key in fields if key in value}
    complete = len(json.dumps(facts, ensure_ascii=False)) <= MAX_FACT_CHARS
    call_id, round_index = _call_id.get() or (None, None)
    record("native", "preparation_observation", call_id=call_id, round=round_index, source=source,
           facts=facts if complete else None, complete=complete)
    return value
