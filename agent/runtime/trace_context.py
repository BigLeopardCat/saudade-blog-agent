"""Record actual model capabilities and turn phases without changing model input."""

import hashlib
import json

from agent.runtime.results import turn_phase
from utils.trace import record


def caller(context):
    record("native", "caller_context", caller=context["caller"],
           permissions=context["permissions"])


def tools(menu, seen):
    # Wire schemas already supplied by bind_tools; never rebuild them for tracing.
    for schema in menu:
        if not isinstance(schema, dict) or not isinstance(schema.get("function"), dict):
            continue
        fingerprint = hashlib.sha256(json.dumps(schema, sort_keys=True).encode()).hexdigest()
        if fingerprint not in seen:
            seen.add(fingerprint)
            record("native", "tool_contract", fingerprint=fingerprint, schema=schema)


def outcome(result):
    execution = result.execution or {}
    phase = turn_phase(result)
    record("native", "turn_state", phase=phase,
           receipt_count=len(execution.get("receipts") or []))
