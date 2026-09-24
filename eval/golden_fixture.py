# -*- coding: utf-8 -*-
"""golden 真写用例的夹具：只读在位检查 + 残留哨兵（20260925）。

## 这个文件为什么存在

`eval/golden/basic.jsonl` 里有第一条**会真写生产库**的用例（`golden_write_category_delete_exec`，
本机即生产：它删掉分类 `agent_fixture_category_a`）。那条用例的安全边界有三条，本文件是其中两条
的实现：

  ① 目标只能是夹具（用例侧 `require_exec_args` 逐字锁参数 + `forbid_exec_tools` 覆盖其余写工具）；
  ② **前置条件**：夹具不在位就不该跑（跑了会红成"站内没有叫 X 的分类"——一个看起来像模型退化、
     其实是前置条件缺失的红）；← 本文件 `fixture_state`
  ③ **残留**：用例失败中途没删掉，夹具就留在生产库里（它同时**在公开分类列表里**，访客看得见）。
     读者不该靠"我记得跑过"来判断，得有个只读的机械检查。← 本文件 `--verify`

## 只有读：这里没有第二条写通道

本模块**只用 `tools.base._get`**（GET，公开接口）——不读任何凭据、不带任何身份、不做任何写。
理由不是洁癖：一条"评测脚本也能写库"的代码路径，等于给真写用例开了第二张许可，而那张许可
不在任何人的授权串里（生产写必须点名「库名+迁移文件」）。这条纪律有**测试机械守着**
（`tests/test_golden_fixture.py` 的源码扫描节：本文件里出现凭据读取/写方法即判红）。

## 判据是"公开接口读得到"

`API_BASE = https://saudade.site/api/public`，`/category` 与用例里 agent 走的那条名通道
（`tools/base._category_index` → `/api/category`）**同一个 handler**（`src/routes/mod.rs:110`）。
所以"在位"的定义就是**访客视角读得到它**：库里插进去了、这个接口读不到 ⇒ 仍然是"不在位"。

## 三种状态，`读不到` 不是 `没有`

同 `_get` 的既有判据（"服务挂了"不许伪装成"查到了、就是空的"）：
`present` / `absent` / `unreadable` 三态分列，**绝不把 unreadable 归成 absent**——
把两者混起来，一次网络抖动就会让用例静默 SKIP（看着"不需要跑"），而真相是我们不知道。

用法：
    .venv/bin/python eval/golden_fixture.py --verify     # 残留哨兵（夜间一行，非门禁）
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.base import _get  # noqa: E402  （全仓评测/检索侧统一的公开读入口）

# 夹具名的保留前缀（与测试账号的 `agent_test_` 同族）。
# **它是一条结构性质**：清场/排查一律可以写成 `... WHERE name LIKE 'agent_fixture_%'`
# （见 `scripts/migration/golden_write_fixture_20260925.sql` 的回滚段）⇒ 不可能误删真数据。
# 用例文件里每一条 `requires_fixture` 的名字必须以此开头，`tests/test_golden_fixture.py`
# 逐条扫着。改这里等于改那条性质，动手前先想清楚。
FIXTURE_PREFIX = "agent_fixture_"

# 残留哨兵的行标（夜间日志/复审时按它 grep）
LEFTOVER_TAG = "[fixture-leftover]"
UNREADABLE_TAG = "[fixture-check-failed]"


def category_titles() -> list[str] | None:
    """公开分类名清单；**读不到返回 `None`**（≠ 空列表，见模块头注的三种状态）。

    走 `_get`：它把上游故障收成 `UPSTREAM_DOWN`（`ToolResult`，kind=unavailable）。
    这里额外判 `isinstance(data, list)`——`ToolResult` 是 `str` 子类，不判它就会把
    一句人话当成"分类名清单"去迭代（真按字符迭代了，`fixture_state` 会安安静静地
    回 absent：又一条"故障伪装成空"）。
    """
    data = _get("/category")
    if not isinstance(data, list):
        return None
    return [str(row.get("categoryTitle") or "") for row in data if isinstance(row, dict)]


def fixture_state(name: str, titles: list[str] | None) -> str:
    """`name` 在不在公开分类列表里 → `present` / `absent` / `unreadable`。

    `titles` **没有默认值，必须显式传**（`category_titles()` 的结果）。这是刻意的：
    默认值只能有一个，而"没传"与"传了 None（读不到）"是两件事——给个 `=None` 的默认值
    就等于把两种情况混成一个，而**"读不到"恰好是这里最要命的那个答案**（见模块头注）。
    调用方一次要判多个夹具名时应当**一次取好传进来**：省往返，也保证几个名字看到的是
    同一份快照。
    """
    if titles is None:
        return "unreadable"
    return "present" if name in titles else "absent"


def leftovers(titles: list[str]) -> list[str]:
    """清单里属于夹具前缀族的所有分类名（排序后返回，便于对账）。

    **只认前缀**（`startswith`），与清场 SQL 的 `LIKE 'agent_fixture_%'` 同一判据：
    名字中间出现该串的东西（`你猜 agent_fixture_x`）清场删不掉，本函数也就不该把它
    报成残留——否则哨兵会对着一条它自己动不了的记录一直响。
    """
    return sorted(t for t in titles if t.startswith(FIXTURE_PREFIX))


def verify(titles: list[str] | None) -> tuple[int, list[str]]:
    """残留哨兵 → `(退出码, 人读行)`。**退出码非零 = 该看一眼**：

      0  公开分类列表里没有夹具族的任何一行（正常态：用例跑完即自我清除）
      1  有残留（`[fixture-leftover]` 逐行点名）——夹具没被删掉，或有人手工建了同族名字
      2  读不到公开接口（`[fixture-check-failed]`）——**无法确认**无残留，不是"没有"

    `titles` 同 `fixture_state`：**没有默认值**（"没传"与"读不到"必须分得开）。
    """
    if titles is None:
        return 2, [f"{UNREADABLE_TAG} 读不到公开分类接口（/category 返回的不是列表）"
                   f"——无法确认夹具残留，不等于没有被残留"]
    left = leftovers(titles)
    if left:
        return 1, [f"{LEFTOVER_TAG} {n}（{len(left)} 行，前缀 {FIXTURE_PREFIX}）"
                   f"—— 真写用例没删掉它，或有人手工建了同族名字；"
                   f"清场见 scripts/migration/golden_write_fixture_20260925.sql 的回滚段"
                   for n in left]
    return 0, [f"[fixture-clean] 公开分类列表 {len(titles)} 个，无 {FIXTURE_PREFIX}* 残留"]


def main(argv: list[str]) -> int:
    if "--verify" not in argv:
        print(__doc__.strip().splitlines()[-1])
        print("用法: python eval/golden_fixture.py --verify")
        return 2
    code, lines = verify(category_titles())
    for line in lines:
        print(line)
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
