# -*- coding: utf-8 -*-
"""实验分支专用读数钩子：把"图看见的上下文有多大"落成一行 JSONL。

**它是什么**：`exp/native-checkpoint` 这条分支的测量仪器，不是功能。原生
checkpoint 一接上、线程一复用，`state["messages"]` 就从"本轮那几条"变成"这个会话
开天辟地以来所有条"——而**没有一处代码会因此报错**（`add_messages` 只是追加）。
所以这件事必须先变成可读的数，再谈结论。

**开关**：环境变量 `CHECKPOINT_PROBE_OUT` 指向一个文件路径。不设 ⇒ `dump()` 立刻
返回，零开销、零行为变化。设了 ⇒ 每个观测点追加一行 JSON。

**为什么住在 agent 包内、又带下划线**：两个观测点长在 `graph.py` 的
`planner_node` / `model_node` 里（那是唯一能同时看见"喂给模型的提示词"和"图的
state"的地方）。臂本身（`agent/checkpoint_arm.py`）只能看见输入与最终 state，
量不到 narrator 那一刻的提示词大小——而那正是 §4.6 说的"上下文无限膨胀"的落点。

**纪律**：本模块**只写文件、绝不改 state、绝不抛**（写失败也只吞掉——测量仪器
不该把一个跑得好好的 golden 用例弄红）。
"""
import json
import os
import threading
import time

_LOCK = threading.Lock()


def out_path() -> str:
    """当前进程的读数落点（空串 = 没开测量）。"""
    return (os.environ.get("CHECKPOINT_PROBE_OUT") or "").strip()


def dump(site: str, **fields) -> None:
    """追加一行读数。`site` = 观测点名字（`planner` / `model`）。"""
    path = out_path()
    if not path:
        return
    rec = {"ts": round(time.time(), 3), "pid": os.getpid(), "site": site}
    rec.update(fields)
    try:
        line = json.dumps(rec, ensure_ascii=False, default=str)
        with _LOCK:
            with open(path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
    except Exception:  # noqa: BLE001 —— 测量仪器不许把用例弄红（见模块头注）
        pass
