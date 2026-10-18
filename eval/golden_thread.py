# -*- coding: utf-8 -*-
"""golden 传给图的 `thread_id`：**坐标只有一处**（20261018 批 6）。

## 为什么它必须是个坐标，而不是一个常量

`eval/run_golden.py` 此前给每一轮传的是**常量** `"golden_thread"`。今天这没有后果——
生产与 golden 都不挂 saver（`agent/checkpoint.py` 的 `build_checkpointer()` 全仓没有
调用方），`thread_id` 是个**死参数**。但它是一条埋在将来的假读数：

  接上 saver 之后，常量线程意味着**所有用例共用同一条线程** ⇒ 用例 N+1 会**恢复**用例 N
  的 state。`add_messages` 是追加 reducer（本仓的消息每轮新建、没有 id ⇒ 永不去重），
  于是症状是"模型退化了"，真相是"上一轮的上下文漏进来了"。

ADR-0011 把这条写成了「不复位就是假读数」，而驱动侧已经照做
（`eval/experiments/ckpt_ab_driver.py::reset_run_threads`：逐用例 × 逐遍 × 逐臂跑之前删
`golden_thread` 与 `conv_<id>` 两处行）。**但那个复位只活在驱动里**——不经驱动的跑法
（夜间 `nightly_regression.sh`、手跑 `run_golden.py`、`golden_case_runner.py`）没有这一层，
而"正确性依赖调用方记得做某一步"正是本仓反复栽过的那一类。本模块把复位换成
**名字里本来就带着坐标**。

## 坐标 = (用例, 重复, 臂)

  · **用例**：`case_id` + `suffix`（重跑那一轮给 `__rerun`）——跨用例不串；
  · **重复**：`GOLDEN_REP`（驱动逐遍跑时设；手跑不设 ⇒ 名字里没有这一段）；
  · **臂**：由调用方从 `eval/golden_arm.py::arm_name()` 取**传进来**，本模块**不读**
    `GOLDEN_ARM`——默认臂名只有那一处事实源，这里再读一遍 env 就是第二个真源
    （连缺省值都得跟着同步，那是同一件事的另一种写法）。

⚠️ **臂必须在名字里**：三条臂读的是**同一个库**（`CHECKPOINT_PG_DSN`），而
`agent/checkpoint_arm.py` 的线程改写只在**有 `conversation_id` 时**才换成 `conv_<id>`
——其余情况原样用本模块给的名字。臂名不在名字里的话，臂 B 与反控 D 在"没有
`conversation_id` 的用例"上会撞进同一条线程，而那正是两臂要分开的那一格。

⚠️ **`conv_<id>` 那条路不受本模块影响，驱动那一步删除仍然必需**：臂 B 的线程是它自己
改写的 `conv_<conversation_id>`，那个坐标**跨遍、跨臂都相同**。本模块让"删除"变得不必需
的只是 `golden_*` 这一族——**改这里不要顺手把驱动那步删掉**。

⚠️ **`LEGACY` 不是给新代码用的**：`run_one` 的 `thread` 参数缺省就是它，为的是**不改变**
那些不走用例主循环的调用方（`eval/zero_call_residual_probe.py` / `eval/arm_capability.py`
等探针直接调 `run_one`，各传各的线程名，其中一个把线程名当自己的坐标用）。golden 的主
循环（`run_case`）**显式**传 `for_case(...)`，不靠缺省。
"""
import os
import re

#: 20261018 之前那个常量。**保留**：它是 `run_one` 的 `thread` 缺省值，也是驱动的
#: `reset_run_threads` 仍在删的那一个（探针调用方还在用它）。
LEGACY = "golden_thread"

#: 逐遍跑时的那一遍编号（`eval/experiments/ckpt_ab_driver.py` 设）。手跑不设
#: ⇒ 名字里没有这一段。
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


def for_case(case_id: str, *, arm: str, suffix: str = "") -> str:
    """`(用例, 重复, 臂)` → 这一轮传给图的 `thread_id`。

    三条不变式（都由 `tests/test_golden_thread.py` 钉着）：
      · **同一个坐标恒同一个名字**——否则复位与写入对不上；
      · **不同用例恒不同名字**——这是本件存在的理由（跨用例不串）；
      · **臂/遍任一不同 ⇒ 名字不同**——两臂、两遍之间不许撞。
    """
    parts = [_PREFIX, _slug(arm), _slug(case_id + suffix)]
    r = rep()
    if r:
        parts.append("r" + _slug(r))
    return "_".join(parts)
