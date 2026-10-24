#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""量 `react` / `tools` 两臂的**前缀缓存命中率**（`llm_done` 缺席的那个洞）。

**为什么要有这个文件**：`cache_read` / `input` 这一对只在 `graph.py` 的生产节点里
`record()` 进 trace；`react_arm` 的决策与叙述节点**一个事件都不记** ⇒ 那两臂的报告里
`planner_*` 三格恒 0，而它们**真的调了 LLM**。20261020 的工具菜单 POC 因此只能跨臂比
挂钟与工具轮数，成本这一栏是空的——而"该不该把生产换成这条线"正是靠成本说话。

**做法（不改任何生产文件）**：进程内给 `ChatOpenAI` 的四个生成入口挂一层 tee，
把每个响应的 `agent.llm_usage.usage_fields()` 收进一个列表，再照 `dial_matrix.token_stats`
的口径算 `cache_read / input`。**只对端点真的回了缓存字段的那些调用算命中率**，
并把 `cache_seen` 一起打出来（分母被悄悄缩小的比率必须看得见）；没量到给 `None` 不给 0。

⚠️ 挂在**类**上而不是实例上：`bind_tools()` 返回的是 `RunnableBinding`，它转发到同一个
模型实例 ⇒ 只有类级补丁能同时盖住"决策那份"和"叙述那份"两个工厂。

用法（**cwd 必须是主仓**，`.env` 是相对路径）：
    GOLDEN_ARM=native PYTHONPATH=<worktree> <main>/.venv/bin/python \
        <worktree>/eval/experiments/usage_probe.py --only <id,id,...>
"""
from __future__ import annotations

import json
import os
import sys
import threading

USAGE: list[dict] = []
_LOCK = threading.Lock()


def _tee() -> None:
    from langchain_openai import ChatOpenAI

    from agent.llm_usage import usage_fields

    def _grab(resp) -> None:
        u = usage_fields(resp)
        if u:
            with _LOCK:
                USAGE.append(u)

    _orig_gen = ChatOpenAI._generate
    _orig_agen = ChatOpenAI._agenerate
    _orig_str = ChatOpenAI._stream
    _orig_astr = ChatOpenAI._astream

    def gen(self, *a, **k):  # noqa: ANN001
        r = _orig_gen(self, *a, **k)
        for g in getattr(r, "generations", []) or []:
            _grab(getattr(g, "message", None))
        return r

    async def agen(self, *a, **k):  # noqa: ANN001
        r = await _orig_agen(self, *a, **k)
        for g in getattr(r, "generations", []) or []:
            _grab(getattr(g, "message", None))
        return r

    def stream(self, *a, **k):  # noqa: ANN001
        for ch in _orig_str(self, *a, **k):
            _grab(getattr(ch, "message", None))
            yield ch

    async def astream(self, *a, **k):  # noqa: ANN001
        async for ch in _orig_astr(self, *a, **k):
            _grab(getattr(ch, "message", None))
            yield ch

    ChatOpenAI._generate = gen
    ChatOpenAI._agenerate = agen
    ChatOpenAI._stream = stream
    ChatOpenAI._astream = astream


def report() -> dict:
    """照 `dial_matrix.token_stats` 的口径：命中率 = `cache_read / input`，缺席不补 0。"""
    n_in = sum(u.get("input", 0) or 0 for u in USAGE)
    n_out = sum(u.get("output", 0) or 0 for u in USAGE)
    cache = sum(u.get("cache_read", 0) or 0 for u in USAGE)
    seen = sum(1 for u in USAGE if u.get("cache_read") is not None)
    return {
        "calls": len(USAGE),
        "input_total": n_in,
        "output_total": n_out,
        "cache_read_total": cache,
        "cache_seen_calls": seen,
        "hit_rate": (round(cache / n_in, 4) if seen and n_in else None),
    }


def main() -> int:
    _tee()
    sys.argv = [sys.argv[0]] + sys.argv[1:]
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    import run_golden

    try:
        rc = run_golden.main()
    except SystemExit as e:      # `main` 内部有直接 `sys.exit(1)` 的收尾分支
        rc = e.code or 0
    out = report()
    out["arm"] = os.environ.get("GOLDEN_ARM") or "native"
    print("\n[usage] " + json.dumps(out, ensure_ascii=False))
    return rc or 0


if __name__ == "__main__":
    sys.exit(main())
