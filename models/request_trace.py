"""Fingerprint the final SDK payload without logging messages or credentials."""

import hashlib
import json

from models.session import current_session
from utils.trace import record


def fingerprint(value, *, sort_keys=False):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=sort_keys)
                          .encode()).hexdigest()


def record_layout(payload, *, endpoint=None):
    messages = payload.get("messages") or []
    tools = payload.get("tools") or []
    # SDK transport options are not generation parameters or prompt content.
    controls = {key: fingerprint(value, sort_keys=True) for key, value in payload.items()
                if key not in ("model", "messages", "tools", "timeout", "extra_headers")}
    record("model", "request_layout", layout_version=2, model=payload.get("model"),
           tools=fingerprint(tools), tool_count=len(tools),
           tool_fingerprints=[fingerprint(tool) for tool in tools],
           controls=fingerprint(controls, sort_keys=True), control_fingerprints=controls,
           endpoint=fingerprint(str(endpoint)) if endpoint else None,
           tools_chars=len(json.dumps(tools, ensure_ascii=False)),
           messages=[fingerprint(message) for message in messages],
           message_chars=[len(json.dumps(message, ensure_ascii=False)) for message in messages],
           roles=[message.get("role") for message in messages],
           tool_choice=payload.get("tool_choice"),
           thinking=payload.get("thinking"), reasoning_effort=payload.get("reasoning_effort"),
           session=fingerprint(current_session()) if current_session() else None)
