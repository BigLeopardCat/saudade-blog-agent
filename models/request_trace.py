"""Fingerprint the final SDK payload without logging messages or credentials."""

import hashlib
import json

from models.session import current_session
from utils.trace import record


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False).encode()).hexdigest()


def record_layout(payload):
    messages = payload.get("messages") or []
    record("model", "request_layout", model=payload.get("model"),
           tools=fingerprint(payload.get("tools") or []),
           messages=[fingerprint(message) for message in messages],
           tool_choice=payload.get("tool_choice"),
           thinking=payload.get("thinking"), reasoning_effort=payload.get("reasoning_effort"),
           session=fingerprint(current_session()) if current_session() else None)
