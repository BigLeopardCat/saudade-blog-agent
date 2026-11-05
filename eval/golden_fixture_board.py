# -*- coding: utf-8 -*-
"""Read-only guestbook fixture presence and state checks through host-issued credentials."""
from __future__ import annotations

import json
import os
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "eval"))

from golden_fixture import FIXTURE_PREFIX  # noqa: E402  （族前缀只有一处来源）

# `talk.approved` 的取值（与 `src/routes/talks.rs` 的三态一一对应）。
# **只在这里出现这一次**：判据是"等于期望值"，不是"> 0 就算通过"。
APPROVED_PENDING = 0

# 读这一份清单的路径（后端 `src/routes/board.rs`，`tools.base._board_index` 同一条）。
BOARD_PATH = "/api/protect/board"

# 残留哨兵的行标（夜间日志/复审时按它 grep）
LEFTOVER_TAG = "[fixture-leftover]"
UNREADABLE_TAG = "[fixture-check-failed]"

# 用例文件的路径（`declared_fixtures()` 要从那里派生"哪些名字是名正言顺的夹具"）
CASES_FILE = ROOT / "eval/golden/basic.jsonl"


def board() -> dict[str, dict] | None:
    """后台留言清单按 talkKey 索引；**读不到返回 `None`**（≠ 空字典，见模块头注）。

    uid 取 `GOLDEN_ADMIN_UID`（与 golden 的身份通道、与账号族同一个环境变量）：本模块
    **不自带** uid，仓库是公开的、环境变量才是投放口。没设 ⇒ `None`（调用方按"不知道"
    处理，而不是拿 uid=0 去试一次注定被拒的请求）。

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
        f"{ADMIN_BASE}{BOARD_PATH}", headers={"Authorization": "Bearer " + token})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = json.loads(resp.read().decode("utf-8", "replace"))
    except Exception:  # noqa: BLE001  （401/403/连接失败/超时：一律"读不到"）
        return None
    # 信封与 `tools.base._principal_get` 同判据：`code != 200` 是**读不到**，不是空清单。
    if not isinstance(body, dict) or body.get("code") != 200:
        return None
    rows = body.get("data")
    if not isinstance(rows, list):
        return None
    out: dict[str, dict] = {}
    for r in rows:
        if isinstance(r, dict):
            out[str(r.get("talkKey"))] = r
    return out


def _matches(board_map: dict[str, dict], name: str) -> list[dict]:
    """Return rows whose content starts with the literal fixture name.

    Underscores in names are literal characters, not SQL LIKE wildcards."""
    return [r for r in board_map.values()
            if str(r.get("content") or "").startswith(name)]


def fixture_state(name: str, board_map: dict[str, dict] | None,
                  expect_approved: int = APPROVED_PENDING) -> str:
    """`name` 这个留言夹具能不能跑 → `present` / `absent` / `wrong_state` / `unreadable`。

    `board_map` **没有默认值，必须显式传**（同另两族的理由："没传"与"传了 None
    （读不到）"是两件事，而"读不到"恰是这里最要命的答案）。
    `expect_approved` 默认待审态（族约定，见模块头注）——要用别的态显式传，别改默认值：
    默认值一改、SQL 那边不同步，就会静默把一条什么都没证明的用例跑成绿。
    """
    if board_map is None:
        return "unreadable"
    rows = _matches(board_map, name)
    if not rows:
        return "absent"
    for r in rows:
        try:
            if int(r.get("approved")) == expect_approved:
                return "present"
        except (TypeError, ValueError):
            continue           # 状态读不出来 = 不知道它是什么态 ⇒ 落到 wrong_state
    return "wrong_state"


def state_label(state: str, name: str) -> str:
    """把状态翻成**可行动**的一句人话（夹具闸的跳过原因就是它）。"""
    if state == "absent":
        return (f"留言夹具不在位（后台留言清单里没有正文以 {name} 打头的行）—— 先按授权串跑 "
                f"scripts/migration/golden_board_fixture_20260930.sql")
    if state == "wrong_state":
        return (f"留言夹具在位、但已不在待审态（{name}）—— 多半是有人在后台把它复核掉了："
                f"重跑 scripts/migration/golden_board_fixture_20260930.sql 重建，"
                f"别放它跑（跑成一条什么都没证明的绿）")
    if state == "unreadable":
        return (f"留言夹具在位检查读不到后台留言清单（{UNREADABLE_TAG}）—— {name} 在不在、"
                f"是什么状态**都不知道**，不跑（清单是管理员域接口，先确认 GOLDEN_ADMIN_UID "
                f"这条身份还活着）")
    return ""


def declared_fixtures() -> list[str]:
    """用例文件里声明的**留言**族夹具名（`requires_fixture_kind: "board"`）。

    **派生，不手写**：残留哨兵要放行的正是"名正言顺的那些夹具"，而谁名正言顺由用例
    说了算——手抄一份名单，早晚出现"用例改了名字、哨兵还在放行旧的"。读不到用例文件
    ⇒ 空列表（哨兵只报前缀族，不做"这个是不是合法夹具"的判断——宁可多报一行，
    不可静默放过）。**去重**保留首次出现顺序（同族夹具常驻，多条用例共用是正常形态）。
    """
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
        if str(case.get("requires_fixture_kind") or "") == "board" and case.get("requires_fixture"):
            name = str(case["requires_fixture"])
            if name not in out:
                out.append(name)
    return out


def leftovers(board_map: dict[str, dict], declared: list[str]) -> list[str]:
    """清单里属于夹具前缀族、**又不是任何用例声明的那个**的正文（排序后返回）。

    与分类族有一处**刻意的不对称**：那边用例跑完夹具就该消失，所以前缀族里出现任何一行
    都是残留；这边夹具是**常驻的**（用例只弹卡、不点确定，见那份 SQL 的"用后即删"），
    所以"声明的那些"必须放行。真正要报的是别的东西——一次中断的 SQL、一次手工插入、
    或者将来某个探针把靶子留在了库里。

    **只认前缀**（`startswith`，与清场 SQL 同一判据）：正文中间出现该串的留言，
    清场语句删不掉它，哨兵也就不该报它。
    """
    out: set[str] = set()
    for r in board_map.values():
        text = str(r.get("content") or "")
        if not text.startswith(FIXTURE_PREFIX):
            continue
        if any(text.startswith(d) for d in declared):
            continue
        out.add(text)
    return sorted(out)


def verify(board_map: dict[str, dict] | None,
           declared: list[str] | None = None) -> tuple[int, list[str]]:
    """残留哨兵 → `(退出码, 人读行)`。退出码语义与另两族逐字一致：

      0  清单里没有夹具族的任何**未声明**行（正常态）
      1  有残留（`[fixture-leftover]` 逐行点名）
      2  读不到清单（`[fixture-check-failed]`）——**无法确认**无残留，不是"没有"
    """
    if board_map is None:
        return 2, [f"{UNREADABLE_TAG} 读不到后台留言清单（{BOARD_PATH} 没读回 data 列表）"
                   f"——无法确认留言夹具有没有残留，不等于没有被残留"]
    names = declared if declared is not None else declared_fixtures()
    left = leftovers(board_map, names)
    if left:
        return 1, [f"{LEFTOVER_TAG} {text[:40]}（{len(left)} 行，前缀 {FIXTURE_PREFIX}）"
                   f"—— 不是任何用例声明的夹具（中断的 SQL / 手工插入 / 探针没清干净）；"
                   f"清场见 scripts/migration/golden_board_fixture_20260930.sql 的回滚段"
                   for text in left]
    # 措辞同账号族：这一行**不检查**声明的夹具在不在位（那是评测侧的夹具闸的事），
    # 列出来的只是"出现也不算残留"的名字。
    return 0, [f"[fixture-clean] 后台留言清单 {len(board_map)} 行，无未声明的 "
               f"{FIXTURE_PREFIX}* 残留（下列名字出现也不算残留：{names or '（无）'}）"]


def main(argv: list[str]) -> int:
    if "--verify" not in argv:
        print("用法: python eval/golden_fixture_board.py --verify")
        print("      （残留哨兵，非门禁；另见 eval/golden_fixture.py --verify 的分类族"
              "与 eval/golden_fixture_account.py --verify 的账号族）")
        return 2
    code, lines = verify(board())
    for line in lines:
        print(line)
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
