# -*- coding: utf-8 -*-
"""IoT 设备族：ESP32 OLED 屏显的读/写工具（经 device-service 下发）。

**为什么单独成模块**（20261017 拆 `tools/base.py`；同一批先切出 `tools/result.py`）：
`tools/base.py` 是全仓第二大体量文件。本族对 `tools/base.py` 的**出边为零**——它只用
stdlib / `httpx` / `langchain_core` / `config.settings` / `utils.logging.get_trace_id`，
再加上 `tools/result.py` 里的信封构造器（`ok` / `tgt` / `empty` / `fact` / `unavailable`
是零出边的叶子，直接从这里 import、**不经过 base**）⇒ 搬得动，且搬完不成环：
`tools/base.py` 在自己那一族原来的位置 import 本模块，本模块**一个字都不 import base**。

**它是什么**：`list_devices` 读设备列表；`device_oled_display` 写（下发屏显命令）。
`_sign_user_jwt` 用与博客相同的 `JWT_SECRET` 签 HS256（sub=user_id，5 分钟有效），
device-service 据此鉴权并校验设备归属——**用户只能操作自己的设备**。
`_DISPLAY_DEDUP_SECONDS` / `_last_display` / `_last_display_lock` 是屏显幂等去重
（多轮重试 / 客户端重试 / MQTT QoS1 at-least-once 重投 ⇒ 同内容只发一次）。

**搬家方式**：`tools/base.py` 按**原名** re-export 本族全部 10 个顶层名
⇒ 管理族那条路径（`_sign_local_jwt` 也签同一把 `JWT_SECRET`）与全仓
`from tools.base import ...` 一处不改。`JWT_SECRET` / `_device_get_user_id` 被管理族
**借走**（入边）——入边不为零不要紧，按原名再导出即可；**出边为零才是选刀判据**。

⚠️ 别丢注释：`device_oled_display` 里那段**回执确认**是**唯一**能分辨"设备真确认了"与
"我们根本没去问"的代码（20261012 补的 WARNING 是**日志口径**的补丁，不是行为修正，
返回语义逐字不变，判据见 `tests/test_silent_degradation.py` ④）；`_last_display` 那把锁
的理由（裸 dict 的 check-then-act 会让同一条屏显下发两次）也在这段注释里。

⚠️ **两处折行**：`device_oled_display` 的 `device_id` 注解与"入队未回执"那句回执文案，
在 `tools/base.py` 里都是**超 100 列的历史长行**（那边的 E501 是存量豁免）。本文件按
`docs/lint-baseline.md` 纪律 1「新增文件零 ruff error」折了行 ⇒ 该函数**行数 121→124**，
`eval/complexity_baseline.json` 里那一条随之重登记为 124（**`c` 仍是 33，一个字没动**；
字面与逻辑均逐字不变，多出来的只是排版）。
"""

import logging
import threading
import time
from typing import Annotated

import httpx
import jwt  # 签发 device-service 令牌（`_sign_user_jwt`，20261020 换掉手写 HS256）
from langchain_core.runnables.config import RunnableConfig
from langchain_core.tools import tool

from config import settings as _settings
from tools.result import empty, fact, invalid_args, not_found, ok, tgt, unavailable
from utils.logging import get_trace_id

# `__all__` = 本模块**所有不以 `_` 开头**的模块级名字（社区惯例，不留例外；
# `tests/test_module_exports.py` 按这条规则机械判着，漂了就红）。
__all__ = ["logger", "DEVICE_SERVICE_URL", "list_devices", "device_oled_display"]

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# IoT 设备（ESP32 OLED 屏幕显示等，经 device-service 下发）
# ---------------------------------------------------------------------------

# 从 settings 读取（模块顶部已 import，见那里的说明）
DEVICE_SERVICE_URL = _settings.device_service_url


def _jwt_secret() -> str:
    """当前配置里的 HS256 密钥。

    **调用时取，不是导入时快照**（20261019）：此前这里是 `JWT_SECRET = _settings.jwt_secret`
    这样一个模块级常量，而套件按既有纪律打的是 `settings.jwt_secret` 的键桩
    （见 `tests/test_admin_write.py` 头部）——桩打在一个地方、签名读的是另一个地方，
    桩**静默不生效**。改成函数后两边同源，桩就是真的。
    空串的处置在两条签名路径各自的调用点（`_sign_user_jwt` / `_sign_local_jwt`），
    这里只管取。
    """
    return _settings.jwt_secret

# 显示指令幂等去重：同一用户短时间内相同内容的重复下发直接跳过。
# 场景：多轮重复调用（planner 重试/多轮规划） / 客户端重试 / MQTT QoS1
# at-least-once 重投——工具层保证"同内容只发一次"。（曾防后端强制路由
# _force_display 与自主调用双调，20260828 影子系统事故后强制路由已移除。）
_DISPLAY_DEDUP_SECONDS = 30.0
# ⚠️ 20260917 加锁：此前是裸 dict 的 check-then-act——两个并发请求可以同时通过
# "30s 内没发过"的检查，同一条屏显下发两次（外部审计指出；影响面只在这条幂等优化
# 本身，不是越权）。execute 跑在线程池里、工具会被并发调用，所以这个锁是必需的。
_last_display: dict[int, tuple[str, float]] = {}
_last_display_lock = threading.Lock()

# ⚠️ 20261012 补观测（同一条线）：`device_oled_display` 里那段**回执确认**是为"幽灵
# 在线窗口"写的（设备断电→遗嘱到达前，曾达 ~2 分钟；publish 入队即 200，设备其实收
# 不到）。它靠响应体里的 `req_id` 起头——而这个 id 解析不出来时，整段确认会被**跳过**，
# 函数照旧返回"已下发"。也就是说：唯一那段能分辨"设备真确认了"与"我们根本没去问"的
# 代码，会在没人知道的情况下不执行，加固等于静默退回原状。所以那一格现在必写一条
# WARNING——**这是日志口径的补丁，不是行为修正**（返回语义逐字不变，判据见
# `tests/test_silent_degradation.py` ④）。


def _sign_user_jwt(user_id: int) -> str:
    """用与博客相同的 JWT_SECRET 签发 HS256 JWT（sub=user_id，5 分钟有效），
    供 device-service 鉴权与设备归属校验（用户只能操作自己的设备）。

    空密钥**不许签**（20261019 安全审计 SAU-012，CWE-321）：HMAC 拿空 key 照样算得出
    签名 ⇒ 这一路会静静地签出一枚"格式完全正确、但谁都能伪造"的令牌。
    与 `tools/base.py::_sign_local_jwt` 是同一条判据的两个入口。

    **20261020 起签发交给 PyJWT**（`jwt.encode`，见 ADR-0014）。**形状一字未改**：
    `{sub, exp, role}` 且**不带 aud**——Rust `auth_jwt::verify_token` 用
    `Validation::default()`，多一个 aud 会被判 audience 无效直接验签失败。
    ⚠️ 与验签侧那条 `verify_sub` 的地雷**方向相反**：这里是**签**，`sub` 本来就该是
    数字（Rust `AssertionClaims.sub: i32`），PyJWT 的 `encode` 不做任何声明校验，
    所以签发侧不受影响——地雷只在 `decode` 上。
    """
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
            # `changed=False`（F1，20260930）：**这一次调用没有向设备下发任何东西**
            # ——同样的内容 30 秒内已经发过了。在事实信封之前，这条回执与"真发了"
            # 长得一模一样（都是同一族的 ok 帧），于是两个读端都把它当成一次真改动：
            # gate 洞⑩ 会把 narrator 照抄回执的那句"这块内容刚刚已经在屏幕上了"判成
            # 谎打回；planner 下一轮还会为同一件事再规划一次。这是本工具里语义最
            # 明确的一处 `changed`——防重去重本来就是"等于没做"。
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
        # device_id 未指定时自动选择第一个在线设备（多步工具链是 IoT 工具失败的
        # 结构性原因：模型无法从 schema 知道运行时才能获取的 device_id，参数缺失时
        # 倾向文本声称而非如实失败。单步化后模型一次调用即成功）
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
        # 回执确认（图改进Ⅴ）：幽灵在线窗口（断电→遗嘱到达前，曾达 ~2 分钟）内下发
        # 会"假成功"——publish 入队即 200，设备实际收不到。轮询 device-service 的
        # 回执状态接口，5s 内设备回执即确认执行，否则如实告知"未确认"，不再承诺已显示。
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
            # 入队成功但没等到设备回执：**指令确实发出去了**（`changed=True`），
            # 但"屏幕上到底变了没有"没被确认——措辞与 `changed` 说的是两件事，
            # 别把后者读成"设备已显示"。
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
        # **没真正下发就把占位撤掉**：否则一次失败（设备离线/HTTP 错/身份缺失）会把
        # 30s 内的正常重试也一并挡掉——那是加锁时最容易引入的行为回归。
        # 撤之前核对占位还是自己那一条，别误撤别人后来占的。
        if not sent:
            with _last_display_lock:
                if _last_display.get(uid) == (text, now):
                    _last_display.pop(uid, None)
