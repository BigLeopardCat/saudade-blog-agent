#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Verify current actor metadata through host-issued tool credentials; never mint login tokens."""
import json
import time
import urllib.error
import urllib.request

# 三态（返回值第一个元素）。字符串常量而不是 Enum：报告要直接落进 JSON。
OK = "ok"
UNUSABLE = "unusable"
UNKNOWN = "unknown"

# 只读、管理员域、负载极小（一次 SELECT，返回主人那几行待办）。选它而不是
# /api/protected/notes/list 是因为那条要扫全表文章，而这里只想问一句"你是谁"。
PROBE_PATH = "/api/protected/todos"


def classify(*, role_expected: str, status: int | None) -> tuple[str, str]:
    """把一次探测的 HTTP 状态码判成 (状态, 说明)。**纯函数**——离线测的就是它。

    `status=None` 表示根本没拿到响应（连接被拒/超时）。"""
    if status is None:
        return UNKNOWN, "读不到后台（连接失败或超时）"
    if status == 401:
        return UNUSABLE, ("令牌被拒（账号不存在 / 已被冻结 / 令牌已被收回）"
                          "—— 这批用例拿不到身份，红出来的话与模型无关")
    if status == 403:
        if role_expected == "admin":
            return UNUSABLE, "该 uid 不是管理员（角色不符）—— 后台读工具会被 403 挡"
        return OK, "活的普通用户（角色符合这条通道的期望）"
    if status == 200:
        if role_expected == "admin":
            return OK, "活的管理员"
        return UNUSABLE, "该 uid 是管理员，但这条通道要的是普通用户（角色不符）"
    return UNKNOWN, (f"前置信道返回 HTTP {status}（不是 auth_guard 的三种形态之一——"
                     "按「不知道」处理，照跑）")


def probe(uid: int, *, role_expected: str, timeout: int = 10,
          retries: int = 2) -> tuple[str, str]:
    """以 `uid` 的身份打一次管理员域只读接口，返回 `classify` 的结论。

    重试只给「读不到」用（部署/重启会让 3000 短暂不可达，一次误判就把十几条用例摘掉
    太贵了）；401/403 是后端明确回答，不重试。
    """
    try:
        # 与真链路同源：同一个签名函数、同一个后台地址。lazy import 的理由同
        # admin_write_probe（import 顺序/副作用），且 import 失败本身要能报出来。
        from tools.base import ADMIN_BASE, host_credential
    except Exception as exc:  # Credential setup errors leave identity unverified.
        return UNKNOWN, f"无法获取主机委托或后台地址（{exc}）"
    token = host_credential(int(uid))
    url = f"{ADMIN_BASE}{PROBE_PATH}"
    status: int | None = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(
                url, headers={"Authorization": "Bearer " + token})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                status = resp.status
        except urllib.error.HTTPError as exc:
            status = exc.code
        except Exception:
            status = None
        if status is not None:
            break
        if attempt < retries:
            time.sleep(1)
    return classify(role_expected=role_expected, status=status)


def main() -> int:
    """命令行单独验一次（不经过 golden）：`.venv/bin/python eval/identity_preflight.py 721 admin`。"""
    import sys
    if len(sys.argv) < 3:
        print("用法: identity_preflight.py <uid> <admin|user>")
        return 2
    state, detail = probe(int(sys.argv[1]), role_expected=sys.argv[2])
    print(json.dumps({"state": state, "detail": detail}, ensure_ascii=False))
    return 0 if state == OK else 1


if __name__ == "__main__":
    raise SystemExit(main())
