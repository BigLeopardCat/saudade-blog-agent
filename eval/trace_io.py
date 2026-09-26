# -*- coding: utf-8 -*-
"""trace 文件**读取**的单一实现（20260926）。

**为什么单列一个模块、而不塞进 `trace_files.py`**：那个模块立了一条刻意的断言
（`tests/test_trace_retention.py` ⑧「只 import os/glob/re、不含 json」）——枚举器不该有
IO 语义，谁都能零成本引它。所以**枚举归 `trace_files.py`、读取归这里**：两个模块都只吃
路径参数、不依赖应用配置（离线测试 / 夜间巡检 / 对账哨兵 / golden 草稿共用）。

**为什么 loader 必须只有一份**：此前这三份逐字相同的实现分别长在 `trace_alert.py` /
`trace_metrics.py` / `trace_reconcile.py` 里，`golden_draft.py` 再借道 `trace_alert`
取用——**`.gz` 那一支漏掉任何一个，那个脚本就静默少看一半语料**。这不是假想：
20260926 一次即席扫描用裸 `json.load` 读"全量 948 份 trace"，862 份 `.gz` 全部抛异常
被跳过，于是"948 份里只有 1 条"实际是"86 份里 1 条"、真值 3 条（外加 78 条命令轮）。
**任何按语料统计的判据，都只能经由这里读文件。**
"""
import gzip
import json


def load_trace(path: str) -> dict | None:
    """读一份 trace（`.json` 与 `.json.gz` 都认）；坏文件返回 None，不抛。

    「读不到」与「里面没有那件事」是两件事：调用端一律按前者处理，不许把 None 当成
    "这份 trace 里没有我要找的东西"（扫描不能因为一个坏文件中断，也不能因此改口径）。
    """
    try:
        if path.endswith(".gz"):
            return json.loads(gzip.decompress(open(path, "rb").read()))
        return json.load(open(path))
    except Exception:
        return None
