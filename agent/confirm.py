# -*- coding: utf-8 -*-
"""Signed, expiring confirmation tokens bound to a user and conversation.

Tokens carry concrete authorized arguments and a random operation ID. They must
never enter prompts, traces, receipts or logs. Missing secrets, invalid signatures,
expired tokens and unresolved argument references fail closed.

Every token pins a registered executor and a positive user/conversation coordinate.
Authorization, frozen-target validation and durable execution remain domain responsibilities.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import secrets
import time

from agent.confirmation_args import has_refs as has_refs
from config import settings

# Domain separation prevents reuse of signatures from other JWT-secret consumers.
_DOMAIN = b"saudade-confirm-v1"
# Includes a 2,000-character announcement with four-byte Unicode, plus metadata.
MAX_TOKEN_CHARS = 16384
TTL_SECONDS = 600
_VERSION = 2


def _secret() -> bytes:
    """Return the independent confirmation key; absence disables signing and verification."""
    return (settings.confirmation_secret or "").encode()


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _b64d(text: str) -> bytes:
    pad = "=" * (-len(text) % 4)
    raw = base64.b64decode(text + pad, altchars=b"-_", validate=True)
    if _b64e(raw) != text:
        raise ValueError("Non-canonical confirmation encoding")
    return raw


def invalid_trace_meta(uid: int, conv_id, token_len: int) -> dict:
    """Trace a rejected confirmation without retaining its token or payload."""
    return {
        "message": "",
        "has_image": False,
        "memory_strategy": "framework_compaction",
        "history_len": 0,
        "has_exec": False,
        "has_confirm": True,
        "conversation_id": conv_id,
        "confirm_rejected": True,
        "confirm_token_len": int(token_len),
    }


def token_expiry(token: str) -> int:
    """Read the signed token's expiry for display; malformed tokens return zero.

    This does not verify a signature and cannot authorize execution."""
    if not token or not isinstance(token, str):
        return 0
    parts = token.split(".")
    if len(parts) != 2:
        return 0
    try:
        payload = json.loads(_b64d(parts[0]).decode())
    except Exception:
        return 0
    exp = payload.get("exp") if isinstance(payload, dict) else None
    return exp if type(exp) is int else 0


def token_jti(token: str) -> str:
    """Read the operation ID for display and persistence bookkeeping.

    This does not verify a signature. Execution uses the ID returned by verify()."""
    if not token or not isinstance(token, str):
        return ""
    parts = token.split(".")
    if len(parts) != 2:
        return ""
    try:
        payload = json.loads(_b64d(parts[0]).decode())
    except Exception:
        return ""
    jti = payload.get("jti") if isinstance(payload, dict) else None
    return jti if isinstance(jti, str) else ""


def narrow(payload: dict, pick: str) -> tuple[dict | None, str]:
    """Select signed operation units without expanding their granted scope.

    Empty selection keeps the original grant. Malformed grouping, indexes or selections
    fail closed. Linked specs remain together; tokens without units use one spec per unit."""
    raw = (pick or "").strip()
    if not raw:
        return payload, ""
    if not isinstance(payload, dict):
        return None, "payload 不是对象"
    specs = payload.get("specs")
    if not isinstance(specs, list) or not specs:
        return None, "令牌里没有可挑选的清单"
    units = _units_of(payload, len(specs))
    if units is None:
        return None, "令牌里的分组读不懂"
    m = re.fullmatch(r"pick:(\d{1,3}(?:,\d{1,3}){0,49})", raw)
    if not m:
        return None, f"选择记号读不懂（{raw[:32]}）"
    indexes = [int(value) for value in m.group(1).split(",")]
    if len(set(indexes)) != len(indexes) or any(index >= len(units) for index in indexes):
        return None, "选择重复或超出已确认清单"
    indexes.sort()
    picked = [specs[i] for index in indexes for i in units[index]]
    if not picked or not all(isinstance(s, dict) for s in picked):
        return None, "选择不包含有效操作"
    out = dict(payload)
    out["specs"] = picked
    out["selection"] = {"selected_units": len(indexes), "total_units": len(units),
                        "unselected": "retained_not_queued"}
    if payload.get("units") is not None:
        cursor = 0
        out["units"] = []
        for index in indexes:
            size = len(units[index])
            out["units"].append(list(range(cursor, cursor + size)))
            cursor += size
    return out, ""


def units_of(payload: dict) -> "list | None":
    """Return validated signed operation groups, or None for malformed grouping."""
    specs = payload.get("specs") if isinstance(payload, dict) else None
    if not isinstance(specs, list) or not specs:
        return None
    return _units_of(payload, len(specs))


def _units_of(payload: dict, n: int) -> "list | None":
    """Validate the complete, non-overlapping operation grouping; malformed input returns None."""
    units = payload.get("units")
    if units is None:
        return [[i] for i in range(n)]
    if not isinstance(units, list) or not units:
        return None
    flat: list = []
    for u in units:
        if not isinstance(u, (list, tuple)) or not u:
            return None
        for k in u:
            if not isinstance(k, int) or isinstance(k, bool) or k < 0 or k >= n:
                return None
            flat.append(k)
    return units if sorted(flat) == list(range(n)) else None


def _valid_binding(uid, conv_id, executor) -> bool:
    if (type(uid) is not int or not 0 < uid <= 2_147_483_647
            or type(conv_id) is not int or not 0 < conv_id <= 2_147_483_647):
        return False
    from agent.domains.registry import executor_domain

    return executor_domain({"executor": executor}) is not None


def sign(
    uid: int,
    conv_id,
    workflow: str,
    specs: list,
    units: "list | None" = None,
    *,
    executor: str,
) -> str:
    """Sign frozen arguments for one actor, conversation and registered executor.

    Malformed bindings, unresolved references and missing keys return an empty token.
    Optional groups define which operation specs must stay together during selection.
    """
    secret = _secret()
    if (not secret or not _valid_binding(uid, conv_id, executor)
            or not isinstance(workflow, str) or not workflow
            or not isinstance(specs, list) or not specs or has_refs(specs)):
        return ""
    payload = {
        "v": _VERSION,
        "uid": uid,
        "conv": conv_id,
        "exp": int(time.time()) + TTL_SECONDS,
        # Independent random IDs prevent same-turn preparations from sharing execution claims.
        "jti": secrets.token_hex(16),
        "workflow": workflow,
        "specs": specs,
        "executor": executor,
    }
    if units:
        payload["units"] = [list(u) for u in units]
    try:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
    except (TypeError, ValueError):
        return ""
    sig = hmac.new(secret, _DOMAIN + body, hashlib.sha256).digest()
    token = _b64e(body) + "." + _b64e(sig)
    return token if len(token) <= MAX_TOKEN_CHARS else ""


def verify(token: str, uid: int, conv_id) -> dict | None:
    """Verify canonical encoding, signature, lifetime and the complete executor binding."""
    secret = _secret()
    if not secret or not isinstance(token, str) or not token or len(token) > MAX_TOKEN_CHARS:
        return None
    parts = token.split(".")
    if len(parts) != 2:
        return None
    try:
        body = _b64d(parts[0])
        sig = _b64d(parts[1])
    except Exception:
        return None
    expect = hmac.new(secret, _DOMAIN + body, hashlib.sha256).digest()
    if not hmac.compare_digest(sig, expect):
        return None
    try:
        payload = json.loads(body.decode())
    except Exception:
        return None
    if (not isinstance(payload, dict) or type(payload.get("v")) is not int
            or payload["v"] != _VERSION
            or not _valid_binding(uid, conv_id, payload.get("executor"))
            or not _valid_binding(payload.get("uid"), payload.get("conv"), payload.get("executor"))
            or payload["uid"] != uid or payload["conv"] != conv_id):
        return None
    jti = payload.get("jti")
    if not isinstance(jti, str) or re.fullmatch(r"[0-9a-f]{32}", jti) is None:
        return None
    exp = payload.get("exp")
    if type(exp) is not int or exp < int(time.time()):
        return None
    if (not isinstance(payload.get("workflow"), str) or not payload["workflow"]
            or not isinstance(payload.get("specs"), list) or not payload["specs"]
            or has_refs(payload["specs"])):
        return None
    return payload


def inspect(token: str) -> dict | None:
    """Decode an untrusted payload for evaluation and diagnostics, without verification.

    Never use this helper for authorization. Only verify() checks the signature, expiry,
    identity and conversation binding. Do not log tokens or decoded production payloads."""
    if not token or not isinstance(token, str):
        return None
    parts = token.split(".")
    if len(parts) != 2:
        return None
    try:
        payload = json.loads(_b64d(parts[0]).decode())
    except Exception:
        return None
    return payload if isinstance(payload, dict) else None
