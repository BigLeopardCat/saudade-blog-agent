# -*- coding: utf-8 -*-
"""Device discovery and ESP32 OLED commands through device-service.

Short-lived actor JWTs let the service enforce device ownership. Display commands
use a locked, short deduplication window. Enqueued commands and device-confirmed
execution remain distinct; missing request IDs and lost acknowledgments are logged."""

import logging
import threading
import time
from typing import Annotated

import httpx
import jwt
from langchain_core.runnables.config import RunnableConfig
from langchain_core.tools import tool

from config import settings as _settings
from tools.result import empty, fact, invalid_args, not_found, ok, tgt, unavailable
from utils.logging import get_trace_id

# Public exports; helper names stay private to their modules.
__all__ = ["logger", "DEVICE_SERVICE_URL", "list_devices", "device_oled_display"]

logger = logging.getLogger(__name__)


# Device-service integration.

# 从 settings 读取（模块顶部已 import，见那里的说明）
DEVICE_SERVICE_URL = _settings.device_service_url


def _jwt_secret() -> str:
    """Read the current signing configuration at call time, including test overrides."""
    return _settings.jwt_secret

# Suppress repeated display content per actor within a short window.
_DISPLAY_DEDUP_SECONDS = 30.0
# The deduplication claim is atomic because tools may run concurrently.
_last_display: dict[int, tuple[str, float]] = {}
_last_display_lock = threading.Lock()

# A missing request ID prevents acknowledgment polling and must be logged explicitly.
# Enqueue success does not establish that the physical device executed the command.


def _sign_user_jwt(user_id: int) -> str:
    """Sign a five-minute HS256 actor JWT for device-service ownership checks.

    Reject an empty key. Numeric sub and absent aud match the verifier contract;
    PyJWT encode preserves those claim types without applying decode validation."""
    secret = _jwt_secret()
    if not secret:
        raise RuntimeError("JWT_SECRET 未配置（空密钥不许代签令牌）——检查 .env / 环境变量")
    return jwt.encode({
        "sub": user_id,
        "exp": int(time.time()) + 300,
        "role": "user",
    }, secret, algorithm="HS256")


def _device_get_user_id(config: RunnableConfig) -> int:
    """从运行时 config 取对话用户 id（server.py 注入 configurable.user_id）。"""
    return int(config.get("configurable", {}).get("user_id") or 0)


def _valid_device_id(device_id: str) -> bool:
    return bool(device_id) and all(c.isalnum() or c in "-_" for c in device_id)


@tool
def list_devices(config: RunnableConfig) -> str:
    """列出当前登录用户拥有的 IoT 设备（ESP32 等），返回设备 id、名称、在线状态。"""
    uid = _device_get_user_id(config)
    if uid <= 0:
        return unavailable("无法获取当前用户身份，设备列表不可用")
    try:
        resp = httpx.get(
            f"{DEVICE_SERVICE_URL}/api/devices",
            headers={"Authorization": "Bearer " + _sign_user_jwt(uid)},
            timeout=10,
        )
        if resp.status_code == 401:
            return unavailable("设备服务认证失败（JWT 无效或过期）")
        devices = resp.json()
        if not devices:
            return empty("当前用户还没有绑定任何 IoT 设备")
        return "\n".join(
            f"- id={d.get('id')} 名称={d.get('name')} 在线={'是' if d.get('online') else '否'}"
            for d in devices
        )
    except Exception as e:
        return unavailable(f"查询设备列表失败: {e}")


@tool
def device_oled_display(
    text: Annotated[str, "要在 ESP32 OLED 屏幕上显示的文字内容"],
    config: RunnableConfig,
    device_id: Annotated[
        str | None, "设备 id（可选；不填时自动选择当前用户第一个在线设备）"
    ] = None,
) -> str:
    """在 ESP32 OLED 小屏幕上显示一段文字（经 MQTT 指令实时下发到设备）。
    指令下发后设备即收到，实际显示效果以设备端为准。"""
    # 注意：config 必须是精确的 RunnableConfig 类型（无默认值）——框架按类型注入，
    # 写成 Optional[RunnableConfig] 会破坏注入导致拿不到 user_id
    uid = _device_get_user_id(config)
    if uid <= 0:
        return unavailable("无法获取当前用户身份，指令未下发")
    if not text or len(text) > 64:
        return invalid_args("显示内容为空或超过 64 字符限制")
    # 幂等去重：30s 内相同用户相同内容不重复下发（防多轮重复调用、QoS1 重投）
    now = time.time()
    with _last_display_lock:
        prev = _last_display.get(uid)
        if prev and prev[0] == text and now - prev[1] < _DISPLAY_DEDUP_SECONDS:
            # No command was sent; changed=False records the observed deduplication no-op.
            return ok("该内容刚刚已下发过，无需重复下发（执行结果以设备回执为准）",
                      meta=fact("device_display", changed=False,
                                target=tgt("device", device_id, text[:20]),
                                before="同一内容 30 秒内已下发", after="未重复下发",
                                text=text))
        # **下发前就占位**（不是发完再记）：占位与检查在同一把锁里，两个并发请求
        # 只有一个能过——这正是原来那版缺的一步。失败路径会在下面把它撤掉。
        _last_display[uid] = (text, now)
    sent = False
    try:
        # When no device is specified, select the first online device returned by the service.
        if not device_id:
            resp = httpx.get(
                f"{DEVICE_SERVICE_URL}/api/devices",
                headers={"Authorization": "Bearer " + _sign_user_jwt(uid)},
                timeout=10,
            )
            devices = resp.json()
            online = [d for d in devices if d.get("online")]
            chosen = (online or devices)[0] if devices else None
            if chosen is None:
                return empty("当前用户还没有绑定任何 IoT 设备")
            device_id = chosen.get("id")
            if not device_id:
                return unavailable("设备列表返回异常，无法获取设备 id")
        if not _valid_device_id(device_id):
            return invalid_args("设备 id 格式非法")
        # 链路关联（图改进Ⅳ）：请求 trace_id 透传 device-service（其日志/回执带同一 id）
        headers = {"Authorization": "Bearer " + _sign_user_jwt(uid)}
        tid = get_trace_id()
        if tid and tid != "-":
            headers["X-Request-Id"] = tid
        resp = httpx.put(
            f"{DEVICE_SERVICE_URL}/api/devices/{device_id}/cmd",
            headers=headers,
            json={"type": "display", "text": text},
            timeout=10,
        )
        if resp.status_code == 404:
            return not_found("设备不存在或不属于当前用户")
        if resp.status_code == 409:
            return unavailable("设备当前不在线，无法显示该内容（设备可能断电或 MQTT 连接断开）")
        if resp.status_code != 200:
            return unavailable(f"指令下发失败（HTTP {resp.status_code}）: {resp.text[:100]}")
        sent = True          # 已真正下发 ⇒ 占位保留，30s 内的重复调用会被去重
        # Poll the request ID for device acknowledgment rather than equating enqueue with execution.
        rid = None
        try:
            rid = resp.json().get("req_id") or None
        except Exception:   # 解析不出 ⇒ 回执确认整段跳过（后果与留痕见 `_last_display` 段）
            logger.warning("回执 id 解析失败 ⇒ 跳过回执确认（按已下发返回）", exc_info=True)
        if rid:
            for _ in range(5):
                time.sleep(1)
                try:
                    st = httpx.get(
                        f"{DEVICE_SERVICE_URL}/api/devices/{device_id}/cmd/{rid}",
                        headers={"Authorization": "Bearer " + _sign_user_jwt(uid)},
                        timeout=5,
                    )
                    if st.status_code == 200 and st.json().get("acked"):
                        return ok("OLED 显示指令已下发，设备已确认执行（回执已记录）",
                                  meta=fact("device_display", changed=True,
                                            target=tgt("device", device_id, text[:20]),
                                            before="原屏文", after="屏幕已显示",
                                            evidence="设备已确认执行", text=text))
                    if st.status_code == 401:
                        break  # 查询鉴权失效，不再等待
                except Exception:
                    break  # 查询接口异常，不再等待
            # The command was enqueued, but device acknowledgment remains unknown.
            return ok("指令已入队下发，但设备未在 5 秒内回执确认——设备可能已断电或"
                      "MQTT 连接断开，请稍后到设备控制台确认",
                      meta=fact("device_display", changed=True,
                                target=tgt("device", device_id, text[:20]),
                                before="原屏文", after="指令已入队",
                                evidence="未收到设备回执", text=text))
        return ok("OLED 显示指令已下发",
                  meta=fact("device_display", changed=True,
                            target=tgt("device", device_id, text[:20]),
                            before="原屏文", after="指令已下发", text=text))
    except Exception as e:
        return unavailable(f"指令下发失败: {e}")
    finally:
        # Release the deduplication claim if no command was sent so a later retry remains possible.
        if not sent:
            with _last_display_lock:
                if _last_display.get(uid) == (text, now):
                    _last_display.pop(uid, None)
