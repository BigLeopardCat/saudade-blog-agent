"""Labels for canonical article and moderation action values."""

from __future__ import annotations

import re

_COLOR_CANON: dict[str, str] = {
    "#1677ff": "蓝色", "#52c41a": "绿色", "#fa8c16": "橙色", "#eb2f96": "粉色",
    "#722ed1": "紫色", "#13c2c2": "青色", "#f5222d": "红色", "#a0d911": "黄绿色",
}


def match_tag_color(spec) -> str | None:
    """Display validated hex values from the typed tag action contract."""
    if not isinstance(spec, str):
        return None
    value = spec.strip().lower()
    return value if re.fullmatch(r"#[0-9a-f]{6}", value) else None


def color_cn(hexval: str) -> str:
    """色板 hex → 规范中文名；色板之外 → 空串（**不编名字**）。"""
    return _COLOR_CANON.get(str(hexval or "").strip().lower(), "")


def describe_color(hexval: str) -> str:
    """hex → 人话（`粉色（#eb2f96）`）；色板外只有色值可给。"""
    name = color_cn(hexval)
    return f"{name}（{hexval}）" if name else str(hexval or "")


STATUS_CN = {"public": "公开", "private": "私密", "draft": "草稿"}


def normalize_status(value) -> str | None:
    """Display only canonical states accepted by the article action contract."""
    return value if isinstance(value, str) and value in STATUS_CN else None


def normalize_top(value) -> int | None:
    """Read the host's boolean/0/1 flag without interpreting natural language."""
    if isinstance(value, bool):
        return int(value)
    return value if type(value) is int and value in (0, 1) else None


BOARD_VERDICT_CN = {"pass": "通过", "reject": "驳回"}


def normalize_verdict(value) -> str | None:
    """Display only verdicts accepted by the moderation action contract."""
    return value if isinstance(value, str) and value in BOARD_VERDICT_CN else None
