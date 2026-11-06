#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""本地钟面的**唯一**格式器。"""
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
