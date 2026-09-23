# -*- coding: utf-8 -*-
"""写操作确认令牌（20260921，隐藏确认请求）。

## 这个文件解决什么

写操作的三道门是"授权 / 确认 / 目标有据"。确认原本只有一条通道：**用户再发一条
消息说"确认"**——那烧掉一整轮 planner+narrator，而且在前端呈现为一条新的用户请求
（用户原话：「会被认为是再次请求」）。现在的通道是：agent 随回复下发一个**待办令牌**，
前端弹一个"确定/取消"的窗，点确定就带令牌发一条**隐藏请求**（不进历史、不起气泡），
agent 跳过 planner 直接执行。

## 为什么是签名令牌，而不是"待办表"

uvicorn 跑 **2 个 worker**：进程内的一张 pending 表在另一个 worker 上根本不存在
（用户点了确定、请求落到另一个 worker = 查不到 = 确认永远失败，还是间歇性的，
最难查的那种）；落库则要一次迁移。签名令牌把状态放在**令牌自身**（服务端零状态），
谁受理都能验。

## 安全语义（本文件存在的全部理由，改任何一条都要重新想清楚）

  · 令牌 = **一次已授权的写的等价物**：不落 trace、不进日志、不进回执、不进 prompt。
  · 绑定 `uid` + `conversation_id` + 签发时刻，TTL `TTL_SECONDS`（10 分钟）。
  · 密钥空缺（配置没读到）时**既不签也不验**——绝不降级成"无签名令牌"。
  · 验签失败一律 `None`（签名不符 / 过期 / 换人 / 换会话 / 格式坏 / 版本不符），
    调用方据此**零执行**。
  · **specs 里不允许残留 `$ref`**（`agent/refs.py` 的 `$tool[N].field`）：引用依赖
    的是"签发那一轮的工具帧"，而执行发生在下一轮，帧早已不在——留着引用等于执行
    时参数解析失败（最好的情况）或解析成别的东西（最坏的情况）。检出即**不签发**，
    退回"如实追问"，见 `has_refs`。

## 纯函数、无网络、无 LLM

`test_confirm.py` 全部秒级复跑。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time

from config import settings

# 域分隔前缀：同一个 `jwt_secret` 在别处也用于签 JWT（`X-Agent-Assertion`），
# 加前缀避免"拿一个 JWT 当确认令牌用"这类跨用途混淆——两边即使撞上同一个密钥，
# 摘要值也不会撞。
_DOMAIN = b"saudade-confirm-v1"

# 令牌有效期（秒）。10 分钟：够用户读完回复、想一下再点；短到"昨天那个框还在屏幕上"
# 时已经失效。取消不发请求，令牌自然过期——这是"取消"零副作用的实现方式。
TTL_SECONDS = 600

# 令牌版本（将来改 payload 结构时 +1，旧版本一律验不过，而不是"尽力解析"）
_VERSION = 1


def _secret() -> bytes:
    """签名密钥；空缺返回 b""（调用方据此拒绝签/验）。"""
    return (settings.jwt_secret or "").encode()


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _b64d(text: str) -> bytes:
    pad = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + pad)


def invalid_trace_meta(uid: int, conv_id, token_len: int) -> dict:
    """**被拒**的确认请求落 trace 时用的 input 元数据（纯函数，`test_confirm.py` 锁住）。

    为什么是纯函数 + 单测而不是就地写个 dict：这里同时是两条纪律的**唯一展开点**——
      · "哪次被拒了"必须事后查得到（在此之前这条路径连 trace 都没有，见 server.py
        `_record_invalid_confirm` 的头注）；
      · "令牌绝不进 trace/日志/回执/prompt"必须查不出来（模块头注的第一条安全语义）。
    只记 `token_len` 是刻意的：长度足够复现"客户端到底有没有把令牌发全"，而长度
    本身不是凭据。`uid` 只用来做 trace 的归属，不参与内容。
    """
    return {
        "message": "",                      # 隐藏请求的合成文本不是主人说的话
        "has_image": False,
        "needs_summary": False,
        "history_len": 0,
        "has_exec": False,
        "has_confirm": True,                # 是"点确定"那一跳
        "conversation_id": conv_id,
        "confirm_rejected": True,           # 验签没过 ⇒ 零执行
        "confirm_token_len": int(token_len),
    }


def has_refs(specs) -> bool:
    """specs 里有没有残留的 `$ref`（见模块头注：有引用就不签发）。

    实现只有一份（`agent.refs.has_refs`，递归进 list/dict）——这里保留同名薄壳，
    是因为调用方读的是本模块的语义（"令牌能不能签"），而不是引用模块的语法。
    """
    from agent.refs import has_refs as _has_refs  # 局部导入：避免模块级循环
    return _has_refs(specs)


def sign(uid: int, conv_id, skill: str, specs: list) -> str:
    """签发待办令牌。密钥空缺 / 参数不全 → **空串**（调用方据此不弹窗）。

    `specs` = `[{"tool": 工具名, "args": {...}}]`，参数必须是**已实例化的具体值**
    （见 `has_refs`）。`skill` 是技能名，执行轮据此拼计划——**不靠模型回忆**。
    """
    secret = _secret()
    if not secret or not skill:
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
        "skill": str(skill),
        "specs": specs or [],
    }
    try:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
    except (TypeError, ValueError):
        return ""  # 参数里有不可序列化的东西（不该发生）→ 不签发
    sig = hmac.new(secret, _DOMAIN + body, hashlib.sha256).digest()
    return _b64e(body) + "." + _b64e(sig)


def verify(token: str, uid: int, conv_id) -> dict | None:
    """验签 → payload dict；任何一条不满足 → `None`（fail-closed，调用方零执行）。

    逐条对应模块头注里的绑定关系：签名（含版本）/ 过期 / 换人 / 换会话。
    `conv_id` 为 None（旧客户端无会话）时只接受 `conv` 同为 None 的令牌——
    "签发时没有会话、执行时也没有"是自洽的，不能让一个绑了会话的令牌在
    无会话请求里生效。
    """
    secret = _secret()
    if not secret or not token or not isinstance(token, str):
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
    if not payload.get("skill") or not isinstance(payload.get("specs"), list):
        return None
    return payload
