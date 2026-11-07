#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Shared local wall-clock formatting."""
from datetime import datetime

# Order matches datetime.weekday(), starting with Monday.
WEEKDAYS = ("星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日")

# Public format shared by context and tool results.
TIME_FORMAT = "%Y年%m月%d日 {wd} %H:%M"


def format_local_now(now: datetime | None = None) -> str:
    """Format the supplied local time, defaulting to the current system clock."""
    now = now or datetime.now()
    return now.strftime(TIME_FORMAT.format(wd=WEEKDAYS[now.weekday()]))
