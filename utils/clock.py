#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""本地钟面的**唯一**格式器（20261018 审计 A2 收敛出来的）。

要回答的问题：「现在几点」在本仓有两个消费方，而它们此前**各写了一遍**：

  · `tools/base.py::get_current_time`（工具；planner 手里没有时间锚时调它）；
  · `server.py::_build_messages` 的 `current_time=` 系统上下文（20260902 时间锚，
    那一场「05:29 的会话在 13:34 续接，模型编出『现在 05:34』」事故的补丁）。

两处**必须逐字同形**：模型把系统上下文里的时刻与工具返回的时刻当**同一个事实**，
格式差一个字符就可能被读成两个来源、进而"两个时间对不上"。而此前是「两张星期表 +
两条 f-string」——按本仓 20260928 审计的说法，这正是头号失效模式（人工同步）的标本。

**判据不写在本文件的注释里**：`tests/test_truth_source_parity.py` 拿两个消费方的
**真实产出**逐字比对（不是比字面），所以这里不需要"记得两处一起改"这种靠人记住的约定。
"""
from datetime import datetime

# 顺序即 `datetime.weekday()` 的 0..6（星期一 = 0）。**别重排**——下标就是契约。
WEEKDAYS = ("星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日")

# 本仓对外的统一钟面：`2026年10月18日 星期日 05:03`。
# 它是**跨语言可见形状**（系统上下文 / 工具返回 / golden 用例里的字面量都长这样），
# 改它等于改契约 —— `tests/test_entities.py` 那条 `2026年09月20日 星期日 19:30`
# 的夹具字面量会当场红，那就是提醒你去看 golden。
TIME_FORMAT = "%Y年%m月%d日 {wd} %H:%M"


def format_local_now(now: datetime | None = None) -> str:
    """本地钟面字符串。`now` 只给测试注入用（不传就是此刻）。"""
    now = now or datetime.now()
    return now.strftime(TIME_FORMAT.format(wd=WEEKDAYS[now.weekday()]))
