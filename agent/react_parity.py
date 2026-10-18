# -*- coding: utf-8 -*-
"""第二条臂**借生产那几件资产**的唯一入口（20261019）。

## 这份文件是什么

`agent/react_arm.py` 与 `agent/graph.py` 是同一件事的两种**循环形状**。差异只该是形状：
同一句话进来，两边该做出同一个决定、弹同一张卡、说同一句拒绝。但 react 臂是**故意
欠配**地长起来的（它自己的文档写着"省下的是手写图的代码量，付出的是这六件"），于是有
一批判据在它身上**根本不存在**——不是判错了，是没判。空集在"纯否定断言族"（`forbid_*`）
上恒真，所以那些读数当年并不作废、只是**没在比同一件事**。

补齐的方式**不是抄一遍**：生产那几件（确认轮拼计划、写目标拒绝链、剔空纠偏、收尾去重、
任务登记……）已经在 `agent/graph.py` 里了，逐条抄进本臂等于把同一份判据养成两份，
而本仓最贵的那类事故正是"改了一处、另一处还在旧口径，**且没有任何东西会红**"。
所以本模块只做一件事：**把那些函数 import 进来，按本臂的 state 形状喂给它们**。

## 三条纪律

  ① **`agent/graph.py` 一个字节都不改**（只读复用）。这条不只是"少动生产"——它还保证
     这次对照的**自变量只剩循环形状**：两边跑的是同一份判据代码。任何"顺手把生产那个
     函数改得更好喂"的冲动都要先落到生产那一侧单独提交，否则 A/B 的结论说不清是谁的。
  ② **判据逻辑不许写在这里**。本模块只做**适配**（本臂的 state → 生产函数的入参形状）
     与**转发**。写在这里就又成了第二份，而且第二份连"抄"的名义都没有了。
  ③ **对不上就响亮**。生产的函数签名/字段集漂了（比如 `_DecideCtx` 加一格），这里要么
     当场炸、要么有锁测试钉住；**不许静默降级成"这一格没接"**——静默降级正是本模块要
     消灭的那件事本身。

## 与 `agent/react_line.py` 的分工

`react_line.py` 装**本臂自己长出来的**那几件（收敛判据、工具级回执、工作流执行器）；
本模块装**从生产借来的**那几件。前者改在那边，后者改在 `agent/graph.py`——本模块只接线。

## 各段的接法与理由

逐段写在各段自己的头注里（它们是判据的一部分，不是背景说明）：确认轮 §A、上下文装配 §B、
五条快道 §C、写目标拒绝链 §D、剔空纠偏 §E、收尾去重 §F、任务登记 §G。
"""
from __future__ import annotations

import logging

from agent.deterministic_plan import _wrap_up_plan
from agent.graph import _confirm_grant_plan
from agent.prompt_context import _has_frames

logger = logging.getLogger(__name__)

# 确认轮受阻那一支的注记（措辞与生产 `graph._planner_preflight` 那一格逐字相同）。
#
# **为什么抬成模块级常量、不直接写在字典字面量里**：`eval/wordlist_gate.py` 的穷举量
# 那一层数的是"中文串住在集合字面量里"的数量——一条**措辞**进 dict 也会被计成"新增
# 词表"。它确实不是词表，但把它抬出来比给闸门加豁免诚实：豁免是判据替自己打的补丁，
# 每加一处就少看见一块（那条闸的头注写着这句话）。
_CONFIRM_WRAP_REASON = "确认轮执行受阻，不重发清单"


# ══════════════════════════════════════════════════════════════════════════════
# §A 确认轮：照签名令牌确定性拼计划（零 LLM）
# ══════════════════════════════════════════════════════════════════════════════
def confirm_round(grant: dict | None, rounds: int, blocked: bool,
                  messages: list) -> tuple[dict, str, dict] | None:
    """确认轮 → `(plan_obj, trace 事件名, trace 载荷)`；**不是确认轮 ⇒ None**。

    生产那一格在 `graph._planner_preflight` 里，四条语义逐字照搬：

      ① **令牌在场且没在续跑 ⇒ 零 LLM**。这是"隐藏确认请求"这条通道的全部意义：不花
         一次 planner 决策，也不给模型"重新理解一遍主人想要什么"的机会——它只该执行
         签名里那件事，一个字都不许改。
      ② **只在首轮（`rounds == 0`）走**。确认轮执行受阻时控制权会回到 planner，那时若
         再照令牌拼一次同一份清单，就是把同一件写操作**做第二遍** ⇒ 第二轮一律转确定性
         收尾，由 narrator 拿着真回执如实说结果。一句话点头的含义是"这一件，办一次"，
         不是一张可反复使用的通行证（`graph._grant_covers` 的 docstring 记着两次现场）。
      ③ **受阻回环不算续跑**（`blocked` 为真时 `resumed` 为假）。受阻那一支照旧确定性
         收尾——令牌那件事没做成，更要紧的是别让模型在这一轮重新规划同一件写。
      ④ **技能与工具对不上就一个都不执行**（`_confirm_grant_plan` 内部判），返回的是
         空清单 + 如实告知；路由照"有清单去 execute、没清单去 model"分流，所以空清单
         那一支会去 narrator，而不是去 execute 空转一轮。

    ⚠️ **返回值是三元组而不是只回 plan_obj**：trace 事件名与载荷也属于这条判据的形状
    （`eval` 侧按事件名读"这一轮是确认轮还是确认收尾轮"）。把它留在调用方现写，就等于
    让"哪条分支记哪个事件"有第二个产地——而两条分支的事件名与载荷**刻意不同**
    （`confirm_grant` 记 workflow/tools；`confirm_wrap` 记 rounds/reason），
    分开写迟早会对错。
    """
    if not grant:
        return None
    # 与生产 `resumed = bool(grant) and rounds > 0 and not state.get("blocked")` 同式。
    if rounds > 0 and not blocked:
        return None
    if rounds == 0:
        plan_obj = _confirm_grant_plan(grant)
        plan_obj["calls"] = signed_calls(plan_obj)
        return plan_obj, "confirm_grant", {"workflow": plan_obj["workflow"],
                                           "tools": plan_obj["tools"]}
    plan_obj = _wrap_up_plan(_has_frames(messages), reason=_CONFIRM_WRAP_REASON)
    return plan_obj, "confirm_wrap", {"rounds": rounds, "reason": _CONFIRM_WRAP_REASON}


def signed_calls(plan_obj: dict) -> list[dict]:
    """确认轮计划的 `calls`：**逐字**把签名体的清单交给 execute，**不走实例化**。

    `calls` 是本臂独有的键（生产用 `plan_view` 读 `plan_obj`），它是"这一轮做什么"
    从 planner 交到 execute 的唯一载体（见 `react_arm.planner_node` 的 docstring：
    决策不靠消息表）。所以任何一条产计划的出口都得把它填上，否则 execute 拿到空清单
    ——形态是"计划里有清单、一件都没跑"，而**没有任何东西会红**（这正是本批要消灭的
    那类静默）。

    `specs` 这一格（而不是 `params`）是刻意的：它的存在就是在告诉 execute
    "**清单已经定稿，别重新实例化**"（理由见 `react_line.WorkflowExecutor.run_specs`
    的 docstring）。确认轮走的必须是这条路——令牌签的是工具与参数，重跑一遍
    `instantiate_plan` 等于把"签的是什么"和"跑的是什么"分开。
    """
    return [{"workflow": str(plan_obj.get("workflow") or ""), "params": {},
             "specs": [str(t) for t in (plan_obj.get("tools") or [])]}]


def plan_has_tools(plan_obj: dict) -> bool:
    """这一份计划有没有调用清单 ⇒ 路由去 execute 还是去 model。

    与 `graph.route_after_planner` 同一句判据（`plan["tools"]`）。这里重写了一遍**表达式**
    而没有复用生产的函数，是因为生产那个读 `plan_view(state)`（一个只存在于生产 state
    形状里的视图），而本臂的计划对象就在手上。**判据相同、取值来源不同**——这正是
    "适配"与"抄判据"的分界：判据是"有没有清单"，不是"从哪读它"。
    """
    return bool(plan_obj.get("tools"))
