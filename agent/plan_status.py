# -*- coding: utf-8 -*-
"""计划状态闭集（`plan["status"]`）：**系统确定性知道的处境**，不是模型自评。

**为什么单独成模块**（20261017 批 2 刀 2c）：这三个常量原先住在 `agent/workflows.py`，
而它们的消费方**一个都不在** workflows 里——`agent/plan_codec.py`（写端校验取值）、
`agent/graph.py`（gate 按值选如实文案、站内"没有"豁免）、`agent/decisions.py`、
`eval/corpus_invariants.py`。定义跟着消费方走，不跟着"碰巧住在一起"走。

**刻意不并进 `agent/plan_codec.py`**：那边 import `agent.workflows`（`WORKFLOW_MAP`），
而参数层（`agent/workflow_params.py`）也要读这个闭集 ⇒ 放进去会绕出一圈
`workflow_params → plan_codec → workflows → workflow_params`。本模块**零仓内依赖**
（连标准库都不用 import），谁都能 import，所以它必须住在环外面。

⚠️ 值本身是**跨文件契约**：`agent/graph.py` 按值选文案、`eval/corpus_invariants.py`
按值统计、`tests/test_status_judgements.py` 逐值判。改一个字符串就是改行为。
"""

from __future__ import annotations

__all__ = [
    "PLAN_STATUS_ABSENCE_EXEMPT",
    "PLAN_STATUS_NAV_NOTE",
    "PLAN_STATUS_VALUES",
]


# ── 计划状态（plan["status"]，20260926 批 3）────────────────────────────
# 这不是"模型自评"，而是**系统确定性知道的处境**：造计划的那一刻
# （`instantiate_plan` / `_terminal_plan` / `_param_problem_plan`）就知道本轮是
# "要调工具"还是"目标已下线"。此前 gate 判这类事只能 grep 注记里的「不调用任何
# 工具」四个字——**判据挂在措辞上**，谁改一句文案谁就把判据悄悄关掉，而且关得无声
# （不报错、不误伤，只是再也不拦）。闭集八值，每个对应一种系统已知的处境：
#   executed           计划里有工具（这一轮要做的事就是执行）
#   answer_only        纯作答轮（chat 技能，本来就不该有工具）
#   param_missing      参数不齐（缺参/值归不了）——`_param_problem_plan`
#   target_unreachable 目标页不存在（navigate 白名单外路径）
#   nav_offline        目标页已下线（NAV_MAP 显式标记）
#   nav_iot_off        物联网平台**本站未部署**（可选件没装，20261002）——与
#                      `nav_offline` 分开的理由是**话术不同**：一个是"曾经有过、撤了"，
#                      一个是"从来没有、可选件没装"。混用一个值的后果在 gate 那一侧：
#                      它按值选兜底文案，"未部署"会被套上"确实已经下线了"
#   nav_unresolved     导航目标认不出来（NAV_MAP 与模糊归一都没命中）
#   refused            明确拒绝/办不成（写技能的 fail-closed 出口）
#   wrapped            确定性收尾轮（轮次上限/复盘终局/剔空收尾）
# **刻意不叫 `plan_status`**：路线图 D2 的 `execution_log` 里另有一个 status 列
# （跨轮执行记忆的落库字段，记的是"那次执行的结果"）；这里是计划字典内部的键
# `plan["status"]`，记的是"这一轮计划自身的处境"。同名不同物，注释里点破。
PLAN_STATUS_VALUES = ("executed", "answer_only", "param_missing",
                      "target_unreachable", "nav_offline", "nav_iot_off",
                      "nav_unresolved", "refused", "wrapped")
# gate 的两处判据读这几个值（`graph.gate_node` 第 4 节选如实文案、
# `graph._claim_issue` 的站内"没有"豁免）。*_NAV_NOTE 是"零工具的 navigate 注记轮"
# 这一个集合——旧判据就是它，判的是措辞；现在判的是这里的值。
PLAN_STATUS_NAV_NOTE = ("nav_offline", "nav_iot_off",
                        "target_unreachable", "nav_unresolved")
PLAN_STATUS_ABSENCE_EXEMPT = PLAN_STATUS_NAV_NOTE + ("param_missing", "refused")
