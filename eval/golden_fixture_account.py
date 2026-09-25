# -*- coding: utf-8 -*-
"""**账号**族真写夹具的只读在位检查（20260926）。

## 为什么另开一个文件，而不是塞进 `golden_fixture.py`

分类族那半（`golden_fixture.py`）的纪律是**零凭据**：它只走 `tools.base._get` 这条公开读
路径，源码扫描测试机械守着（出现 `Authorization` / `_sign_local_jwt` / 写方法即判红）。
账号族**做不到**那条纪律，而且不是"懒得做"：

  · 后台账号名录 `GET /api/temp-users` 是**管理员域**接口（`auth_guard` 挡着），全站没有
    任何一条公开读能看到"某个账号在不在、是不是冻结态"；
  · 而夹具的在位判据只能是它——用例要动的那一行，正是名录里那一行。

所以这条读路径与 `eval/identity_preflight.py` 同源：**自签一枚只读的管理员令牌**
（`tools.base._sign_local_jwt`，与 agent 代调完全同一个函数），打一次 GET，**只看，不写**。
两条纪律分别由两个测试守着（分类族：不带身份；账号族：不带写动词），见
`tests/test_golden_fixture.py` 的 ④ 与 ⑦。

⚠️ 这里**不是**第二条写通道：本模块只有 GET，源码扫描把 `.post(` / `/status` / `frozen`
这些写形状全列为禁止项。改这个文件之前先读 `docs/security-boundary.md` 与
`scripts/migration/golden_fixture_account_20260926.sql` 的头注：生产写的授权串是
「用户点名库名+迁移文件」，不因为"评测脚本也需要"而多出第二张许可。

## 族约定：账号夹具**建出来就是冻结态**

`agent_admin_fixture` 族（前缀沿用 `golden_fixture.FIXTURE_PREFIX`）里的账号，一律由
`scripts/migration/golden_fixture_account_20260926.sql` 插成 `status=1`（冻结）。
这不是本模块的猜测，是**文件与用例之间的契约**：唯一消费它的用例
（`account_unfreeze_exec`）做的就是"把它解冻"，所以"它现在是冻结的"正是这条用例能证明
任何东西的前提。

**为什么这个前提非查不可**（假绿的那条路）：用例跑完夹具就变成"正常"了。此时若不复位
再跑一次，工具照样会走完整条写通道——后端那个方向本来就有真 no-op 分支（"该账号已经是
正常状态"→ `ApiResponse::success`），于是**回执照样生成、`require_exec_tools` 照样过**，
用例静默变绿而一个问题都没验。所以期望态不符时本模块判 `wrong_state` ⇒ 用例**响亮跳过**
（`run_golden` 的夹具闸按"不在位"处理并打印复位办法），而不是让它跑成一条空的绿。

## 四种状态，`读不到` 不是 `没有`（与分类族同一条取向）

    present      名录里那一行在，且 `status` 正是期望态 ⇒ 可以跑
    absent       名录里没有这个名字 ⇒ 夹具没建（或被人删了）
    wrong_state  行在、但状态不是期望态 ⇒ 多半是"用例跑完没复位"，重跑迁移 SQL 即可
    unreadable   名录读不回来（没给 uid / 网络错 / 后端拒绝）⇒ **不知道**，不跑

把 `unreadable` 归成 `absent` 的后果与分类族一模一样：一次网络抖动让真写用例静默跳过，
而真相是我们不知道夹具在不在。

用法：
    .venv/bin/python eval/golden_fixture_account.py --verify   # 残留哨兵（夜间一行，非门禁）
"""
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
        # 与真链路同源：同一个签名函数、同一个后台地址（lazy import 同 identity_preflight：
        # import 顺序/副作用，且取不到签名能力本身要能报成"不知道"）。
        from tools.base import ADMIN_BASE, _sign_local_jwt
    except Exception:  # noqa: BLE001
        return None
    token = _sign_local_jwt(int(uid), "admin")
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
    """用例文件里声明的**账号**族夹具名（`requires_fixture_kind: "account"`）。

    **派生，不手写**：残留哨兵要放行的正是"名正言顺的那些夹具"，而谁名正言顺由用例
    说了算——手抄一份名单，早晚出现"用例改了名字、哨兵还在放行旧的"（那种哨兵会对着
    一条真残留一直报，然后被学会忽略）。读不到用例文件 ⇒ 空列表（哨兵只报前缀族，
    不做"这个是不是合法夹具"的判断——宁可多报一行，不可静默放过）。
    """
    try:
        lines = CASES_FILE.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out = []
    for ln in lines:
        if not ln.strip():
            continue
        try:
            case = json.loads(ln)
        except Exception:  # noqa: BLE001
            continue
        if str(case.get("requires_fixture_kind") or "") == "account" and case.get("requires_fixture"):
            out.append(str(case["requires_fixture"]))
    return out


def leftovers(directory: dict[str, dict], declared: list[str]) -> list[str]:
    """名录里属于夹具前缀族、**又不是任何用例声明的那个**的账号名（排序后返回）。

    与分类族的哨兵有一处**刻意的不对称**：那边用例跑完夹具就该消失，所以前缀族里出现
    任何一行都是残留；这边夹具是**常驻的**（用例只改它的状态、不复位就得重跑 SQL），
    所以"声明的那些"必须放行。真正要报的是别的东西——一次中断的 SQL、一次手工插入、
    或者将来某个探针把靶子留在了库里。

    **只认前缀**（`startswith`，与清场 SQL 的 `LIKE 'agent\_fixture\_%'` 同一判据）：
    名字中间出现该串的账号，清场语句删不掉它，哨兵也就不该报它。
    """
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
