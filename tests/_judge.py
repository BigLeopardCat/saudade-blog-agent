# -*- coding: utf-8 -*-
"""判据收集器：**全仓唯一一处实现**（20261021）。

转 pytest 之前，173 个套件各自手抄了一份 `check()`，一共 **15 个版本**（打印用
`✅/❌` 还是 `✓/✗`、失败串里带不带明细，各不相同）。收成一处之后，
「判据怎么打印、失败怎么记」只有一个落点 —— 改口径不必再改 173 个文件。

## ⚠️ 为什么不是「每条判据一个 pytest item」（这条是**量出来的**，不是口味）

判据是**在循环里按运行时数据生成的**，收集期根本不知道有多少条、叫什么：

| 套件 | 静态 `check(` 调用点 | 运行时实际条数 |
|---|---|---|
| `test_authz` | 202 | **269** |
| `test_confirm` | 189 | **211** |
| `test_reports` | 198 | **224** |
| `test_account_freeze` | 123 | **146** |
| `test_hardening` | 65 | **97** |

名字里还有 **558 处 f-string**（`f'{_t} 要 write.console…'`）。⇒ 参数化 item 既不能按
下标（静态条数 ≠ 运行时条数）、也不能按名字（收集期拿不到）静态生成。
**「一个套件 = 一个 item、判据收齐了一起报」是这套语料的正确形状**，
不是"迁移没做完"（见 `docs/eval-observability.md` 的同一段）。

## 用法（每个套件两行，替换掉本地那份 `def check`）

    from _judge import collector

    FAILS: list[str] = []
    check = collector(FAILS)

`FAILS` 仍住在套件自己那层：**累加器是本模块的**，全量进一个进程时不会互相串。
"""

from __future__ import annotations


def collector(fails: list[str]):
    """返回一个 `check(name, cond, detail="")`：打印一行，失败时记进 `fails`。

    三条约定（全仓统一，`tests/test_judge_collector.py` 判着）：

    - **名字在前**：`check(名字, 条件, 明细)`。转 pytest 前有 168 个套件是这个顺序、
      1 个是反的（`test_widget_pin_drift.py`，已按这个顺序改过来）。
    - **记进 `fails` 的那条与打印出来的那一行逐字相同**（明细用 `  [明细]` 跟在名字后）。
      转之前只有 13 个套件记明细、其余只记名字 ⇒ 这一处是**唯一一处故意的不同**，
      方向是"信息更多"（失败清单是给人读的）。
    - **不抛异常**：判据收齐了在尾部一次报（`assert not FAILS, …`），
      这样一个套件跑一次能看见**所有**没过的判据，而不是首败即停。
    """
    def check(name: str, cond: bool, detail: str = "") -> None:
        suffix = f"  [{detail}]" if detail else ""
        print(f"  {'✅' if cond else '❌'} {name}{suffix}")
        if not cond:
            fails.append(f"{name}{suffix}")

    return check
