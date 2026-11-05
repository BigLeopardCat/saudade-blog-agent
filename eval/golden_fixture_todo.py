# -*- coding: utf-8 -*-
"""Read-only presence and state checks for administrator-owned todo fixtures."""
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

# 夹具的**期望完成态**：`false` = 未完成（族约定，见模块头注）。
# 判据是"等于期望值"而不是"> 0 就算做了"——线上口径是 JSON 布尔（`TodoDto.done: bool`），
# 读不出布尔（缺键 / 脏值）一律 `wrong_state`，不拿真值判断的默认值去兜。
EXPECT_DONE = False

# 夹具的**期望排期**（`YYYY-MM-DD`，与那份 SQL 的 `due_date` 逐字同源）。
# 判据同样是"等于期望值"：`date` 读不出来（缺键/脏值）或不是这个日子 ⇒ `wrong_state`。
# **为什么要判它**（这条不是洁癖）：用例的断言里逐字带着这个日子渲染出来的中文
# （`把待办「…」勾成完成（排期 11月30日，现在：未完成）`），而排期**不是模型能编的东西**
# ——它是台账真值。于是"夹具的排期过时了"与"模型没按台账念"会红成同一个样子，而前者是
# 前置条件问题、后者才是回归。判在这里 ⇒ 排期对不上时用例**响亮跳过**（理由里直接说
# 「排期不是约定的那个日子，重跑那份 SQL 的复位段」），而不是红成模型退化。
# 改这个值要三处同步：本文件、父仓 `scripts/migration/golden_fixture_todo_20261003.sql`、
# 用例的 `text_any_regex`。`tests/test_golden_fixture.py` 有锁盯着后两处。
EXPECT_DATE = "2026-11-30"

# 读这份列表的路径（后端 `src/routes/todos.rs::list_todos`）。**带身份**：这是管理员域
TODO_PATH = "/api/protected/todos"

# 残留哨兵的行标（夜间日志/复审时按它 grep）
LEFTOVER_TAG = "[fixture-leftover]"
UNREADABLE_TAG = "[fixture-check-failed]"

# 用例文件的路径（`declared_fixtures()` 要从那里派生"哪些正文是名正言顺的夹具"）
CASES_FILE = ROOT / "eval/golden/basic.jsonl"


def todo_rows() -> list[dict] | None:
    """真管理员那份待办列表；**读不到返回 `None`**（≠ 空列表，见模块头注）。

    uid 取 `GOLDEN_ADMIN_UID`（与 golden 的身份通道、与另两族同一个环境变量）：本模块
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
        f"{ADMIN_BASE}{TODO_PATH}", headers={"Authorization": "Bearer " + token})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = json.loads(resp.read().decode("utf-8", "replace"))
    except Exception:  # noqa: BLE001  （401/403/连接失败/超时：一律"读不到"）
        return None
    # 信封与 `tools.base._principal_get` 同判据：`code != 200` 是**读不到**，不是空列表。
    # （`_todo_rows` 收到的是已经拆过信封的 `data`，这里是自己发的请求 ⇒ 自己拆。）
    if not isinstance(body, dict) or body.get("code") != 200:
        return None
    rows = body.get("data")
    if not isinstance(rows, list):
        return None
    return [r for r in rows if isinstance(r, dict)]


def _matches(rows: list[dict], name: str) -> list[dict]:
    """正文以 `name` 打头的那些行（`name` 是夹具名，族约定 = 正文本身）。

    **前缀而不是相等**：与那份 SQL 的幂等/清场判据逐字一致（`LIKE 'agent\_fixture\_%'`）
    ——检查用的判据与清场用的判据必须是同一条，否则会出现"检查说在、清场删不掉"的行。
    真待办不可能以这个前缀打头，所以多匹配的风险是 0。
    """
    return [r for r in rows
            if str(r.get("text") or "").startswith(name)]


def fixture_state(name: str, rows: list[dict] | None,
                  expect_done: bool = EXPECT_DONE,
                  expect_date: str | None = EXPECT_DATE) -> str:
    """`name` 这条待办夹具能不能跑 → `present` / `absent` / `wrong_state` / `unreadable`。

    `rows` **没有默认值，必须显式传**（同另三族的理由："没传"与"传了 None（读不到）"
    是两件事，而"读不到"恰是这里最要命的答案）。
    `expect_done` 默认未完成（族约定，见模块头注）——要别的态显式传，别改默认值：
    默认值一改、SQL 那边不同步，就会静默把一条什么都没证明的用例跑成绿。
    `expect_date` 同理默认族约定的那个排期；传 `None` = **不判排期**（给将来不留排期的
    夹具用——那时用例的断言里也就不会有日子）。
    """
    if rows is None:
        return "unreadable"
    hits = _matches(rows, name)
    if not hits:
        return "absent"
    # 同名多条 ⇒ 歧义即零写（`_todo_text_hits` / `pick_todo` 两边都会拒）⇒ 用例的卡
    # 弹不出来。它与"这一行已经完成了"是两件事，但在**能不能跑**上是同一个答案。
    if len(hits) > 1:
        return "wrong_state"
    got = hits[0].get("done")
    if not isinstance(got, bool):
        return "wrong_state"      # 完成态读不出来（缺键/脏值）= 不知道它什么态 ⇒ 不能跑
    if got is not expect_done:
        return "wrong_state"
    if expect_date is not None:
        # 排期是**用例断言里的那半个字面量**的来源（见 EXPECT_DATE 的注释）：对不上时
        # 用例会红成"模型没按台账念"，而真相是前提过期了。
        if str(hits[0].get("date") or "").strip() != expect_date:
            return "wrong_state"
    return "present"


def state_label(state: str, name: str) -> str:
    """把状态翻成**可行动**的一句人话（夹具闸的跳过原因就是它）。"""
    if state == "absent":
        return (f"待办夹具不在位（{name} 不在后台首页那份待办列表里）—— 先按授权串跑 "
                f"scripts/migration/golden_fixture_todo_20261003.sql")
    if state == "wrong_state":
        return (f"待办夹具在位、但状态不能跑（{name}）—— 三种成因，都指向同一份 SQL 的复位段"
                f"（scripts/migration/golden_fixture_todo_20261003.sql）：① 它已经被勾成完成"
                f"（那一轮会走「状态已达成 ⇒ 不弹卡」，用例要的卡根本不存在）；② 同名不止"
                f"一条（歧义即零写）；③ 排期不是约定的 {EXPECT_DATE}（用例断言里逐字带着"
                f"这个日子渲染出的中文，对不上会红成「模型没按台账念」——那是假红，前提是"
                f"过期了）。三种都重跑那份 SQL 的复位段，别放它跑")
    if state == "unreadable":
        return (f"待办夹具在位检查读不到后台待办列表（{UNREADABLE_TAG}）—— {name} 在不在一行、"
                f"什么状态**都不知道**，不跑（那份列表是管理员域接口，先确认 GOLDEN_ADMIN_UID "
                f"这条身份还活着）")
    return ""


def declared_fixtures() -> list[str]:
    """用例文件里声明的**待办**族夹具正文（`requires_fixture_kind: "todo"`）。

    **派生，不手写**：残留哨兵要放行的正是"名正言顺的那些夹具"，而谁名正言顺由用例
    说了算——手抄一份名单，早晚出现"用例改了名字、哨兵还在放行旧的"（那种哨兵会对着
    一条真残留一直报，然后被学会忽略）。读不到用例文件 ⇒ 空列表（哨兵只报前缀族，
    不做"这个是不是合法夹具"的判断——宁可多报一行，不可静默放过）。

    **去重**（保留首次出现顺序）：待办夹具是**常驻**的（用例只读它、不改它，见模块头注），
    多条用例共用同一条是正常形态（同账号族那半的理由）。
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
        if str(case.get("requires_fixture_kind") or "") == "todo" and case.get("requires_fixture"):
            name = str(case["requires_fixture"])
            if name not in out:
                out.append(name)
    return out


def leftovers(rows: list[dict], declared: list[str]) -> list[str]:
    """那份列表里属于夹具前缀族、**又不是任何用例声明的那个**的正文（排序后返回）。

    与账号族同一条不对称：待办夹具是**常驻的**（用例只读、不复位也还在），所以"声明的
    那些"必须放行。真正要报的是别的东西——一次中断的 SQL、一次手工插入，
    或者将来某个探针把靶子留在了列表里。

    **只认前缀**（`startswith`，与清场 SQL 的 `LIKE 'agent\_fixture\_%'` 同一判据）：
    正文中间出现该串的行，清场语句删不掉它，哨兵也就不该报它。
    """
    keep = set(declared)
    return sorted({str(r.get("text") or "") for r in rows
                   if str(r.get("text") or "").startswith(FIXTURE_PREFIX)
                   and str(r.get("text") or "") not in keep})


def verify(rows: list[dict] | None,
           declared: list[str] | None = None) -> tuple[int, list[str]]:
    """残留哨兵 → `(退出码, 人读行)`。退出码语义与另三族逐字一致：

      0  列表里没有夹具族的任何**未声明**正文（正常态）
      1  有残留（`[fixture-leftover]` 逐行点名）
      2  读不到列表（`[fixture-check-failed]`）——**无法确认**无残留，不是"没有"
    """
    if rows is None:
        return 2, [f"{UNREADABLE_TAG} 读不到后台待办列表（{TODO_PATH} 没读回列表）"
                   f"——无法确认待办夹具有没有残留，不等于没有被残留"]
    names = declared if declared is not None else declared_fixtures()
    left = leftovers(rows, names)
    if left:
        return 1, [f"{LEFTOVER_TAG} {n}（{len(left)} 行，前缀 {FIXTURE_PREFIX}）"
                   f"—— 不是任何用例声明的夹具（中断的 SQL / 手工插入）；"
                   f"清场见 scripts/migration/golden_fixture_todo_20261003.sql 的回滚段"
                   for n in left]
    # 措辞刻意不写"已声明的夹具（就是它们）"：这一行**不检查**声明的夹具在不在位（那是
    # 评测侧的夹具闸的事），列出来的只是"出现也不算残留"的名字。写成"夹具：X"会被读成
    # "X 在位"——一句哨兵自己没验过的话（同账号族的措辞）。
    return 0, [f"[fixture-clean] 后台待办列表 {len(rows)} 行，无未声明的 "
               f"{FIXTURE_PREFIX}* 残留（下列正文出现也不算残留：{names or '（无）'}）"]


def main(argv: list[str]) -> int:
    if "--verify" not in argv:
        print("用法: python eval/golden_fixture_todo.py --verify")
        print("      （残留哨兵，非门禁；另见 eval/golden_fixture.py --verify 的分类族那一半）")
        return 2
    code, lines = verify(todo_rows())
    for line in lines:
        print(line)
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
