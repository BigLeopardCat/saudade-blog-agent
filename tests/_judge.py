# -*- coding: utf-8 -*-
"""判据收集器：**全仓唯一一处实现**。
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
