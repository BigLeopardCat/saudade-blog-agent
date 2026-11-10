"""Trace explicitly selected, validated service facts within their tool invocation."""

import json
from contextlib import contextmanager

from utils import tool_context
from utils.trace import record

MAX_FACT_CHARS = 8000


@contextmanager
def scope(call, round_index=None):
    with tool_context.scope((call or {}).get("id"), round_index):
        yield


def observed(source, value, fields):
    """Only existing-state fields are selected; proposed text is never evidence."""
    facts = {key: value[key] for key in fields if key in value}
    complete = len(json.dumps(facts, ensure_ascii=False)) <= MAX_FACT_CHARS
    call_id, round_index = tool_context.current()
    record("native", "preparation_observation", call_id=call_id, round=round_index, source=source,
           facts=facts if complete else None, complete=complete)
    return value


def preparation_context(call, round_index, popup):
    """Record product-declared preparation metadata, excluding drafts and grants."""
    facts = popup.get("preparation_facts")
    if facts is None:
        return
    complete = isinstance(facts, dict) and len(json.dumps(facts, ensure_ascii=False)) <= MAX_FACT_CHARS
    record("native", "preparation_context", call_id=call["id"], round=round_index,
           facts=facts if complete else None, complete=complete)
