"""Fingerprint the final SDK payload without logging messages or credentials."""

import hashlib
import json

import httpx

from models.session import current_session
from utils.trace import record

_CONTEXT_SESSION = object()
MAX_WIRE_LAYOUT_BYTES = 1024 * 1024


def fingerprint(value, *, sort_keys=False):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=sort_keys)
                          .encode()).hexdigest()


def record_layout(payload, *, endpoint=None, event="request_layout",
                  observed_session=_CONTEXT_SESSION):
    session = current_session() if observed_session is _CONTEXT_SESSION else observed_session
    messages = payload.get("messages") or []
    tools = payload.get("tools") or []
    # The OpenAI SDK merges extra_body into the JSON request, overriding base fields.
    extra = payload.get("extra_body") or {}
    # SDK transport options are not generation parameters or prompt content.
    controls = {key: fingerprint(value, sort_keys=True) for key, value in payload.items()
                if key not in ("model", "messages", "tools", "timeout", "extra_headers")}
    record("model", event, layout_version=2, model=payload.get("model"),
           tools=fingerprint(tools), tool_count=len(tools),
           tool_fingerprints=[fingerprint(tool) for tool in tools],
           controls=fingerprint(controls, sort_keys=True), control_fingerprints=controls,
           endpoint=fingerprint(str(endpoint)) if endpoint else None,
           tools_chars=len(json.dumps(tools, ensure_ascii=False)),
           messages=[fingerprint(message) for message in messages],
           message_chars=[len(json.dumps(message, ensure_ascii=False)) for message in messages],
           roles=[message.get("role") for message in messages],
           tool_choice=extra.get("tool_choice", payload.get("tool_choice")),
           thinking=extra.get("thinking", payload.get("thinking")),
           reasoning_effort=extra.get("reasoning_effort", payload.get("reasoning_effort")),
           session=fingerprint(session) if session else None)


async def record_wire_request(request):
    """Observe the SDK's merged HTTP JSON and actual routing header, without private values."""
    session = request.headers.get("x-opencode-session")
    try:
        body = request.content
        if len(body) > MAX_WIRE_LAYOUT_BYTES:
            raise ValueError("oversized")
        payload = json.loads(body)
        if (not isinstance(payload, dict)
                or not isinstance(payload.get("messages", []), list)
                or not all(isinstance(row, dict) for row in payload.get("messages", []))
                or not isinstance(payload.get("tools") or [], list)):
            raise ValueError("unsupported shape")
    except (ValueError, httpx.RequestNotRead):
        record("model", "wire_request_unavailable",
               session=fingerprint(session) if session else None)
        return
    record_layout(payload, endpoint=str(request.url.copy_with(query=b"")),
                  event="wire_request", observed_session=session)
