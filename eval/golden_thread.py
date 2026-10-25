"""Stable evaluation coordinates isolate cases, repetitions and reruns."""

import os
import re

LEGACY = "golden_thread"

ENV_REP = "GOLDEN_REP"

_PREFIX = "golden"
_UNSAFE = re.compile(r"[^0-9A-Za-z_]+")


def _slug(s: str) -> str:
    """线程名里那一段的归一化：非 `[0-9A-Za-z_]` 一律折成 `_`。

    为什么要归一化：这个名字会原样写进 PG 的 `thread_id` 列，而复位那一步是
    `DELETE ... WHERE thread_id = %s`——**两处必须是同一个字符串**。任何"看起来一样、
    其实差一个字符"的形态都会变成"复位了但没删掉"，而那个失效是静默的（下一遍接着
    上一遍的 state 跑，读数看起来正常）。用例 id 里真的出现过 `.`/`-`/空格这类字符。
    空串 ⇒ `x`：不编一个像用例名的东西顶上。
    """
    return _UNSAFE.sub("_", s).strip("_") or "x"


def rep() -> str:
    """这一遍的编号（`GOLDEN_REP`）；手跑 ⇒ 空串。"""
    return (os.environ.get(ENV_REP) or "").strip()


def for_case(case_id: str, *, suffix: str = "") -> str:
    """Return the same coordinate for the same case, repetition and suffix."""
    parts = [_PREFIX, "native", _slug(case_id + suffix)]
    r = rep()
    if r:
        parts.append("r" + _slug(r))
    return "_".join(parts)
