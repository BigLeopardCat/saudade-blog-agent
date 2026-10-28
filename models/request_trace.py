"""Fingerprint the final SDK payload without logging messages or credentials."""

import hashlib
import json

from models.session import current_session
from utils.trace import record


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False).encode()).hexdigest()


def record_layout(payload):
    messages = payload.get("messages") or []
    tools = payload.get("tools") or []
    record("model", "request_layout", model=payload.get("model"),
           tools=fingerprint(tools), tool_count=len(tools),
           tools_chars=len(json.dumps(tools, ensure_ascii=False)),
           messages=[fingerprint(message) for message in messages],
           message_chars=[len(json.dumps(message, ensure_ascii=False)) for message in messages],
           roles=[message.get("role") for message in messages],
           tool_choice=payload.get("tool_choice"),
           thinking=payload.get("thinking"), reasoning_effort=payload.get("reasoning_effort"),
           session=fingerprint(current_session()) if current_session() else None)
