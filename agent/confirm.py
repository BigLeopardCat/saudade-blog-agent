# -*- coding: utf-8 -*-
"""Signed, expiring confirmation tokens bound to a user and conversation.

Tokens carry concrete authorized arguments and a random operation ID. They must
never enter prompts, traces, receipts or logs. Missing secrets, invalid signatures,
expired tokens and unresolved argument references fail closed.

The optional signed executor pins migrated operations to their durable execution
path, including revocation and replay checks. Legacy tokens retain their existing
execution path and the host's best-effort one-time claim.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import secrets
import time

from config import settings

# Domain separation prevents reuse of signatures from other JWT-secret consumers.
_DOMAIN = b"saudade-confirm-v1"
# Includes a 2,000-character announcement with four-byte Unicode, plus metadata.
MAX_TOKEN_CHARS = 16384
TTL_SECONDS = 600
_VERSION = 2


def _secret() -> bytes:
    """签名密钥；空缺返回 b""（调用方据此拒绝签/验）。"""
    return (settings.jwt_secret or "").encode()


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
        "message": "",  # 隐藏请求的合成文本不是主人说的话
        "has_image": False,
        "needs_summary": False,
        "history_len": 0,
        "has_exec": False,
        "has_confirm": True,  # 是"点确定"那一跳
        "conversation_id": conv_id,
        "confirm_rejected": True,  # 验签没过 ⇒ 零执行
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
    return exp if isinstance(exp, int) else 0


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


def has_refs(specs) -> bool:
    """specs 里有没有残留的 `$ref`（见模块头注：有引用就不签发）。

    实现只有一份（`agent.arg_refs.has_refs`，递归进 list/dict）——这里保留同名薄壳，
    是因为调用方读的是本模块的语义（"令牌能不能签"），而不是引用模块的语法。
    """
    from agent.arg_refs import has_refs as _has_refs  # 局部导入：避免模块级循环

    return _has_refs(specs)


def narrow(payload: dict, pick: str) -> tuple[dict | None, str]:
    """Select one signed operation unit without expanding its granted scope.

    Empty selection keeps the original grant. Malformed grouping, indexes or selections
    fail closed. Linked specs remain together; tokens without units use one spec per unit."""
    raw = (pick or "").strip()
    if not raw:
        return payload, ""  # 空 = 没有选择 = 全部办（与旧前端逐字兼容）
    if not isinstance(payload, dict):
        return None, "payload 不是对象"
    specs = payload.get("specs")
    if not isinstance(specs, list) or not specs:
        return None, "令牌里没有可挑选的清单"
    units = _units_of(payload, len(specs))
    if units is None:
        return None, "令牌里的分组读不懂"
    m = re.fullmatch(r"pick:(\d{1,3})", raw)
    if not m:
        return None, f"选择记号读不懂（{raw[:32]}）"
    idx = int(m.group(1))
    if idx >= len(units):
        return None, f"选择越界（{idx} / 共 {len(units)} 件）"
    picked = [specs[i] for i in units[idx]]
    if not picked or not all(isinstance(s, dict) for s in picked):
        return None, f"选择越界（{idx} / 共 {len(units)} 件）"
    out = dict(payload)
    out["specs"] = picked
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


def sign(
    uid: int,
    conv_id,
    workflow: str,
    specs: list,
    units: "list | None" = None,
    *,
    executor: str | None = None,
) -> str:
    """签发待办令牌。密钥空缺 / 参数不全 → **空串**（调用方据此不弹窗）。

    `specs` = `[{"tool": 工具名, "args": {...}}]`，参数必须是**已实例化的具体值**
    （见 `has_refs`）。`workflow` 是技能名，执行轮据此拼计划——**不靠模型回忆**。

    `executor` 固定执行入口，使更换默认运行时后仍核对撤销账。

    `units` = `adminops.linked_units(specs)` 的结果：卡面编号与「只办第 N 件」的
    按钮都按它数（见 `narrow`）。不传 ⇒ 载荷里不带这个键，读侧按逐条一单元处理。
    """
    secret = _secret()
    if not secret or not workflow:
        return ""
    # 引用不签发（见模块头注与 has_refs）：这道判断放在**签发点**而不是只放调用方
    # ——"带引用的令牌"是一张到期必然兑现不了的支票，任何将来新增的签发路径都该
    # 被同一道闸挡住，而不是靠每个调用方各自记得检查。
    if has_refs(specs):
        return ""
    payload = {
        "v": _VERSION,
        "uid": int(uid),
        "conv": conv_id if isinstance(conv_id, int) else None,
        "exp": int(time.time()) + TTL_SECONDS,
        # 32 位操作标识，适配宿主 varchar(64) 字段。
        # 每次签发都是新的随机值——**不派生自 uid/会话/时间**，否则同一秒内两次
        # 签发会撞成同一个 jti，第二次会被自己的第一次认领挡掉。
        "jti": secrets.token_hex(16),
        "workflow": str(workflow),
        "specs": specs or [],
    }
    if units:
        payload["units"] = [list(u) for u in units]
    if executor:
        payload["executor"] = executor
    try:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
    except (TypeError, ValueError):
        return ""  # 参数里有不可序列化的东西（不该发生）→ 不签发
    sig = hmac.new(secret, _DOMAIN + body, hashlib.sha256).digest()
    token = _b64e(body) + "." + _b64e(sig)
    return token if len(token) <= MAX_TOKEN_CHARS else ""


def verify(token: str, uid: int, conv_id) -> dict | None:
    """验签 → payload dict；任何一条不满足 → `None`（fail-closed，调用方零执行）。

    逐条对应模块头注里的绑定关系：签名（含版本）/ 过期 / 换人 / 换会话。
    `conv_id` 为 None（旧客户端无会话）时只接受 `conv` 同为 None 的令牌——
    "签发时没有会话、执行时也没有"是自洽的，不能让一个绑了会话的令牌在
    无会话请求里生效。
    """
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
    if not isinstance(payload, dict) or payload.get("v") != _VERSION:
        return None
    if payload.get("uid") != int(uid):
        return None
    got_conv = payload.get("conv")
    want_conv = conv_id if isinstance(conv_id, int) else None
    if got_conv != want_conv:
        return None
    exp = payload.get("exp")
    if not isinstance(exp, int) or exp < int(time.time()):
        return None
    if not payload.get("workflow") or not isinstance(payload.get("specs"), list):
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
