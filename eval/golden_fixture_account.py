# -*- coding: utf-8 -*-
"""Read-only fixture account inventory through host-issued tool credentials."""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "eval"))

from golden_fixture import FIXTURE_PREFIX  # noqa: E402  （族前缀只有一处来源）

# `user.status` 取值域（与 `src/authz.rs` 的 `STATUS_ACTIVE` / `STATUS_FROZEN` 一一对应）。
# **只在这里出现这一次**：判据是"等于期望值"，不是"> 0 就算冻结"——后端对未登记的
# 值一律按冻结处理（失败取向不默认放行），评测侧跟着用同一个数字，而不是另立一套。
STATUS_ACTIVE = 0
STATUS_FROZEN = 1

# 读这一份名录的路径（后端 `src/routes/temp_user.rs::list_temp_users`）。**注意形状**：
# 它是全站唯一**不套 `ApiResponse` 信封**的 `/api/protected` 接口（裸数组）——
# 这里按裸数组解析，别"统一"成 `body["data"]`（改了就是 0 行名录 ⇒ 夹具判 absent ⇒
# 真写用例静默跳过，而接口一直是好的）。
DIRECTORY_PATH = "/api/temp-users"

# 残留哨兵的行标（夜间日志/复审时按它 grep）
LEFTOVER_TAG = "[fixture-leftover]"
UNREADABLE_TAG = "[fixture-check-failed]"

# 用例文件的路径（`declared_fixtures()` 要从那里派生"哪些名字是名正言顺的夹具"）
CASES_FILE = ROOT / "eval/golden/basic.jsonl"


def directory() -> dict[str, dict] | None:
    """后台账号名录 `username → 行`；**读不到返回 `None`**（≠ 空字典，见模块头注）。

    uid 取 `GOLDEN_ADMIN_UID`（与 golden 的身份通道同一个环境变量）：本模块**不自带**
    uid，仓库是公开的、环境变量才是投放口。没设 ⇒ `None`（调用方按"不知道"处理，而不是
    拿 uid=0 去试一次注定被拒的请求）。

    **GET，且只有 GET**：这条读路径的全部风险在于"评测代码也能写库"，所以形状上不留
    余地——不传 body、不带 method 参数（urllib 默认就是 GET）。
    """
    uid = (os.environ.get("GOLDEN_ADMIN_UID") or "").strip()
    if not uid.isdigit() or int(uid) <= 0:
        return None
    try:
        from tools.base import ADMIN_BASE, host_credential
    except Exception:  # noqa: BLE001
        return None
    token = host_credential(int(uid))
    req = urllib.request.Request(
        f"{ADMIN_BASE}{DIRECTORY_PATH}", headers={"Authorization": "Bearer " + token})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            rows = json.loads(resp.read().decode("utf-8", "replace"))
    except Exception:  # noqa: BLE001  （401/403/连接失败/超时：一律"读不到"）
        return None
    if not isinstance(rows, list):
        return None
    return {str(r.get("username") or ""): r for r in rows if isinstance(r, dict)}


def fixture_state(name: str, directory: dict[str, dict] | None,
                  expect_status: int = STATUS_FROZEN) -> str:
    """`name` 这个账号夹具能不能跑 → `present` / `absent` / `wrong_state` / `unreadable`。

    `directory` **没有默认值，必须显式传**（同 `golden_fixture.fixture_state` 的理由：
    "没传"与"传了 None（读不到）"是两件事，而"读不到"恰是这里最要命的答案）。
    `expect_status` 默认冻结态（族约定，见模块头注）——要用"正常态"的夹具时显式传，
    别改默认值：默认值一改，SQL 那边不同步就会静默把一条什么都没证明的用例跑成绿。
    """
    if directory is None:
        return "unreadable"
    row = directory.get(name)
    if row is None:
        return "absent"
    try:
        got = int(row.get("status"))
    except (TypeError, ValueError):
        return "wrong_state"      # 状态读不出来 = 不知道它是什么态 ⇒ 不能跑
    return "present" if got == expect_status else "wrong_state"


def state_label(state: str, name: str) -> str:
    """把状态翻成**可行动**的一句人话（夹具闸的跳过原因就是它）。"""
    if state == "absent":
        return (f"账号夹具不在位（后台账号名录里没有 {name}）—— 先按授权串跑 "
                f"scripts/migration/golden_fixture_account_20260926.sql")
    if state == "wrong_state":
        return (f"账号夹具在位、但状态不是期望的冻结态（{name}）—— 多半是用例跑完没复位："
                f"重跑 scripts/migration/golden_fixture_account_20260926.sql 的复位段，"
                f"别放它跑（跑成一条什么都没证明的绿）")
    if state == "unreadable":
        return (f"账号夹具在位检查读不到后台账号名录（{UNREADABLE_TAG}）—— {name} 在不在、"
                f"是什么状态**都不知道**，不跑（名录是管理员域接口，先确认 GOLDEN_ADMIN_UID "
                f"这条身份还活着）")
    return ""


def declared_fixtures() -> list[str]:
    """Return the distinct account fixture names declared by the corpus.

    Multiple cases may intentionally share the same fixture; deduplicate names
    without changing case counts or eligibility."""
    try:
        lines = CASES_FILE.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out: list[str] = []
    for ln in lines:
        if not ln.strip():
            continue
        try:
            case = json.loads(ln)
        except Exception:  # noqa: BLE001
            continue
        if str(case.get("requires_fixture_kind") or "") == "account" and case.get(
            "requires_fixture"
        ):
            name = str(case["requires_fixture"])
            if name not in out:
                out.append(name)
    return out


def leftovers(directory: dict[str, dict], declared: list[str]) -> list[str]:
    """Return sorted fixture-prefixed account names not declared by any case.

    Prefix comparison is literal; underscores are not SQL LIKE wildcards."""
    keep = set(declared)
    return sorted(u for u in directory if u.startswith(FIXTURE_PREFIX) and u not in keep)


def verify(directory: dict[str, dict] | None,
           declared: list[str] | None = None) -> tuple[int, list[str]]:
    """残留哨兵 → `(退出码, 人读行)`。退出码语义与分类族逐字一致：

      0  名录里没有夹具族的任何**未声明**账号（正常态）
      1  有残留（`[fixture-leftover]` 逐行点名）
      2  读不到名录（`[fixture-check-failed]`）——**无法确认**无残留，不是"没有"
    """
    if directory is None:
        return 2, [f"{UNREADABLE_TAG} 读不到后台账号名录（{DIRECTORY_PATH} 没读回列表）"
                   f"——无法确认账号夹具有没有残留，不等于没有被残留"]
    names = declared if declared is not None else declared_fixtures()
    left = leftovers(directory, names)
    if left:
        return 1, [f"{LEFTOVER_TAG} {n}（{len(left)} 行，前缀 {FIXTURE_PREFIX}）"
                   f"—— 不是任何用例声明的夹具（中断的 SQL / 手工插入 / 探针没清干净）；"
                   f"清场见 scripts/migration/golden_fixture_account_20260926.sql 的回滚段"
                   for n in left]
    # 措辞刻意不写"已声明的夹具（就是它们）"：这一行**不检查**声明的夹具在不在位（那是
    # 评测侧的夹具闸的事），列出来的只是"出现也不算残留"的名字。写成"夹具：X"会被读成
    # "X 在位"——一句哨兵自己没验过的话。
    return 0, [f"[fixture-clean] 后台账号名录 {len(directory)} 行，无未声明的 "
               f"{FIXTURE_PREFIX}* 残留（下列名字出现也不算残留：{names or '（无）'}）"]


def main(argv: list[str]) -> int:
    if "--verify" not in argv:
        print("用法: python eval/golden_fixture_account.py --verify")
        print("      （残留哨兵，非门禁；另见 eval/golden_fixture.py --verify 的分类族那一半）")
        return 2
    code, lines = verify(directory())
    for line in lines:
        print(line)
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
