#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""golden「真实身份」通道的前置在位检查（20260926）。

**为什么要有它。** `GOLDEN_ADMIN_UID` / `GOLDEN_USER_UID` 是 run_golden 用来把
「被 role/权限挡住」与「被 uid 哨兵挡住」分开的唯一凭据（见 run_golden.py 里那条通道
的注释）。凭据会**被用错而不自知**：721 一旦被冻结（20260926 起冻结立刻作废令牌）、
被改密码（代次 +1）、或换成一个角色不符的 uid，十几条真身份用例会**集体变红**——
而红出来的长相与「模型退化」一模一样：复审单上只有模型说的话，看不出前置条件没了。
20260926 的复核把这条列为评测信号的头号混淆源，这就是它的对症药。

**判据不猜。** 拿**与 agent 代调完全同源**的令牌（`tools.base._sign_local_jwt`——
真链路用的就是那个函数，连"不带 `ver` 声明"这条都一致）打一次**管理员域**的只读接口
`/api/protected/todos`，按 `src/middleware.rs::auth_guard` 的三态分：

    200 = 人是活的、且是管理员        ⇒ admin 通道期望的就是它
    403 = 人是活的、但不是管理员      ⇒ **user 通道期望的就是它**（它存在的意义就是这个）
    401 = 令牌被拒：账号不存在 / 已被冻结 / 令牌已被收回
          （后端刻意回 401 不回 403：「人已经不在线了」要触发前端清令牌跳登录，
            而 403 的既有语义是「人还在、只是不该进后台」）

**一个接口同时判两件事**（活着 + 角色），所以两条通道共用同一条判据，只换期望值。

**「不知道」不等于「不可用」**（这条是刻意的，别改成对称的）：
  * `unusable`（后端明确回答"这个身份不行"）⇒ 把这类用例摘掉、**退出码 3**。
    前置条件不满足 ⇒ 这些用例**没被评估**，不是一个通过率，更不是"通过"。
  * `unknown`（读不到：网络错、路由 404、后端正在重启）⇒ **只警告、照跑**。
    那种情况下 golden 用例自己会红得更大声（工具返回 unavailable），而按 unknown 摘掉
    十几条会把真信号一起吞掉——**不知道就不动**在这里会变成"不知道就闭嘴"。

与探针的分工：`scripts/probe_token_revoke.py` 验的是**收回语义**（冻结/改密码真的
作废令牌、旁路真的收口，会写库、要 `--admin-uid`）；本模块只做**一次只读探测**，
是评测开跑前的在位检查。两者都自签令牌，但探针刻意用自己的签名路径（"探针自己的读
路径，与 agent 工具完全独立"）——这里相反，**必须**用 agent 那条，因为它检的就是那条。
"""
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
        # probe_admin_write（import 顺序/副作用），且 import 失败本身要能报出来。
        from tools.base import ADMIN_BASE, _sign_local_jwt
    except Exception as exc:  # 取不到签名能力 ⇒ 不知道，不是不可用
        return UNKNOWN, f"取不到 agent 的签名函数/后台地址（{exc}）"
    token = _sign_local_jwt(int(uid), role_expected)
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
