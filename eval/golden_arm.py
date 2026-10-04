# -*- coding: utf-8 -*-
"""评测的**臂**（arm）：同一份 golden 语料、同一套判据，跑的是哪一套循环。

**为什么要有这个模块**（20261004）：「ReAct 能不能到 95%」这个问题此前**没有任何读数**——
`eval/run_golden.py` 把 `server._agent = create_agent()` 写死在一处，语料/判据/报告形状
全是围着它长出来的，第二条臂**没有入口**。这一层就是那个入口，它只做三件事：

  ① **选臂**：`GOLDEN_ARM` 环境变量（缺省 `graph`）。用 env 而不是 `--arm` 是为了让
     `golden_full_run.py` 的**逐例子进程**零改动地继承它（那层壳不穿 argv 管道）；
  ② **建臂**：`build_agent()` 返回那个对象，调用方照旧写 `server._agent = <它>`——
     producer（`server._run_agent_stream_to_queue`）**一个字节都不改**：它只认四类 update
     与那几族控制帧，不认这循环内部长什么样；
  ③ **分栏**：`engine_for()` / `reports_dir()` 让两条臂的报告**分开读、分开归档**。

三条纪律（动这里之前先读，每条都有一次现场）：

  · **graph 臂的 `engine` 恒为 `"native"`（逐字）**：`eval/baseline_group.py` 按 engine 归档
    历史基线，给它加个后缀会把今天的读数与昨天的基线**劈成两档**（读的人会以为基线没了）；
  · **臂名不进文件名**：`report_archive.open_archive` 的注写着"文件名顺序 = 时间顺序"，
    同位置加后缀就破坏它 ⇒ 臂只体现为**目录**（`runs/` 与 `runs_<臂>/`）；
  · **`last_run.json` 只由 graph 臂写**：它被当成"最近一次基线"读，第二条臂覆盖它 = 把
     基线悄悄换成另一套循环的读数（同 `--only` 那次把它写成 `total=1` 的教训）。

⚠️ 试验臂的实现**不住在本模块**：`build_agent()` 按 `agent.<臂>_arm` 去 import——那样它只
存在于承载它的分支上，而本模块（选臂/分栏这层壳）两边各有一份、不必各自漂移。找不到模块
时**响亮失败**，绝不静默退回 graph：「看着在跑第二条臂、其实在跑第一条」是本仓反复吃过的
那类坑（能力有测试 ≠ 接线有测试）。
"""
import importlib
import os

ARM_GRAPH = "graph"
ARM_REACT = "react"
KNOWN_ARMS = (ARM_GRAPH, ARM_REACT)
ENV_ARM = "GOLDEN_ARM"

# 接口层（报告里 `engine` 那一格的地基，20260927 批 A）：20261004 起只剩 native tool calls
# 一条路（文本契约档连同 `PLANNER_ENGINE` 拨盘一起删了）。**定义成常量而不是从 settings 里
# 读**——没有第二个取值可拨，"读配置"是个假动作，会让读者以为它可变。字面量只此一处：
# `engine_for` 由它派生出每一臂的取值，`run_golden.INTERFACE_LAYER` 是它的再导出
# （历史引用点保持可用，值不再各写一遍）。
INTERFACE_LAYER = "native"


def arm_name() -> str:
    """这一轮跑哪条臂（`GOLDEN_ARM`，缺省 `graph`）。

    **不认识的取值响亮失败**：拼错一个字母就静默换一套循环、报告却照样绿，是这一层最坏的
    失效方式（而且它是一条**假读数**——比报错难查得多）。
    """
    name = (os.environ.get(ENV_ARM) or "").strip().lower() or ARM_GRAPH
    if name not in KNOWN_ARMS:
        raise RuntimeError(
            f"{ENV_ARM}={name!r} 不认识（已知：{'/'.join(KNOWN_ARMS)}）——臂名拼错必须当轮"
            "炸掉，不能静默退回 graph（那会产出一份看着像第二条臂的假读数）")
    return name


def engine_for(arm: str) -> str:
    """报告里 `engine` 那一格。**graph 臂逐字 `native`**（见头注第 1 条纪律）。"""
    return INTERFACE_LAYER if arm == ARM_GRAPH else f"{INTERFACE_LAYER}+{arm}"


def reports_dir(arm: str) -> str:
    """这一臂的留档目录。graph 臂 = `eval/report/runs`（历史归档一个不挪，见第 2 条纪律）。"""
    return "eval/report/runs" if arm == ARM_GRAPH else f"eval/report/runs_{arm}"


def is_baseline_arm(arm: str) -> bool:
    """这一臂能不能当"最近一次基线"被读（= 能不能写 `last_run.json`，见第 3 条纪律）。"""
    return arm == ARM_GRAPH


def build_agent(arm: str | None = None):
    """建这一臂的 agent 对象；调用方照旧 `server._agent = build_agent()`。

    graph 臂走 `agent.create_agent()`（与 `run_golden.ensure_agent` 原来那一行同源，行为
    逐字节不变）；其余臂走 `agent.<臂>_arm.build()`，模块不在本树就**响亮失败**。
    """
    arm = arm or arm_name()
    if arm == ARM_GRAPH:
        from agent import create_agent
        return create_agent()
    try:
        mod = importlib.import_module(f"agent.{arm}_arm")
    except ModuleNotFoundError as e:
        raise RuntimeError(
            f"{ENV_ARM}={arm} 要 `agent.{arm}_arm`，本树里没有这个模块（{e.name}）——试验臂的"
            "实现跟着它自己的分支走，本模块只有选臂/分栏这层壳。**不回退 graph**：那样跑出来的"
            "是一份看起来是第二条臂、其实是第一条的假读数") from e
    return mod.build()
