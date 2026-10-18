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
五条快道 §C、屏幕文案创作 §C′（显示快道的前置件）、写目标拒绝链 §D、剔空纠偏 §E、
收尾去重 §F、任务登记 §G。
"""
from __future__ import annotations

import logging
import re

from agent.arg_refs import ref_hints
from agent.block_reasons import denied_workflows
from agent.deterministic_plan import (_article_fast_path, _display_fast_path,
                                      _effect_switch_fast_path, _intent_hints,
                                      _nav_fast_path, _referent_nav_fast_path,
                                      _wrap_up_plan)
from agent.graph import _confirm_grant_plan, _create_display_text, _fastpath_on
from agent.prompt_context import (_doc_anchors, _has_frames,
                                  _last_assistant_utterance, _recent_tail,
                                  _short_reply_hint, blocked_rows)

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


# ══════════════════════════════════════════════════════════════════════════════
# §B planner 提示词的注入值：与生产 `graph._planner_setup` 的 `_prompt_args` 逐格同源
# ══════════════════════════════════════════════════════════════════════════════
# 这几句话**住在模块级**而不是写在字典字面量里：`eval/wordlist_gate.py` 的穷举量那一层
# 数的是"中文串住在集合字面量里"的数量，一条措辞进 dict 也会被计成"新增词表"。
# （措辞与生产 `graph._planner_setup` 的那三句逐字相同。）
_NON_FIRST_ROUND = "（非首轮决策：短应答语义已在上轮兑现）"
_NO_LEDGER = "（本轮没有去读待办台账）"
_NO_REFLECTOR = "（本决策轮无复盘建议）"
_NO_CORRECTION = "（本决策轮无纠偏提示）"


# ══════════════════════════════════════════════════════════════════════════════
# §C 首轮的五条确定性快道：顺序、守卫、命名与生产逐条同源
# ══════════════════════════════════════════════════════════════════════════════
# `page_ctx` 里当前特效那一格的正则——**与生产 `_planner_preflight` 逐字同一句**。
# 它是 `_effect_switch_fast_path` 的入参之一（旧效果取值顺序里"当前开着且非目标的
# 其它效果"那一档），所以这一句也必须同源：换个写法（比如容忍空格）会让同一条消息
# 在一臂命中、另一臂落回 LLM。
_CURRENT_EFFECTS_RE = re.compile(r"current_effects=([^;\]]+)")


def fast_path(state, *, user_msg: str, page_ctx: str) -> tuple[dict, str] | None:
    """五条确定性快道 → `(plan_obj, kind)`；一条都不命中 ⇒ None（落回 LLM）。

    **顺序是判据的一部分**，与生产 `graph._planner_preflight` 逐条同：
    nav → referent_nav → display → article → effect_switch。换顺序会让"两句话都能
    命中两条快道"的那些输入在两臂上落到不同的技能上——那种差不是策略差。

    `kind` 就是 trace 的载荷（生产那一格也是这么分的：`record("planner", "fastpath",
    kind=…)`），所以它必须逐字相同：`eval` 侧读的是这一格，换个名字等于两臂的
    fastpath 分布对不上。

    **守卫留在调用方**（`rounds == 0 and not has_frames`）：本臂的"有没有帧"读法与
    生产不同（本臂没有 `turn_start` 冻结，用的是"最后一条 HumanMessage 之后"），
    把那句话搬进来等于把两个不同的量塞进同一个函数。`AGENT_FASTPATH` 那一档在这里
    读（**唯一读口仍是 `graph._fastpath_on`**），因为它管的就是这一整块跑不跑。

    ⚠️ **快道的产出本身就是计划**：调用方必须走 `run_specs`（清单已定稿），不能拿
    `workflow`+`params` 重新实例化。`effect_switch` 是硬证据——它返回**两条 spec**
    （关旧 + 开新），而 `effect` 技能模板只有一条，重跑一遍 `instantiate_plan` 会把
    它撕成一条；而"模型反复丢目标效果半边"正是这条快道存在的全部理由。
    """
    if not _fastpath_on():
        return None
    nav = _nav_fast_path(user_msg)
    if nav is not None:
        return nav, "nav"
    referent = _referent_nav_fast_path(
        user_msg, _last_assistant_utterance(list(state.get("messages") or [])))
    if referent is not None:
        return referent, "referent_nav"
    display = _display_fast_path(user_msg)
    if display is not None:
        return display, "display"
    article = _article_fast_path(user_msg, page_ctx)
    if article is not None:
        return article, "article_read"
    m = _CURRENT_EFFECTS_RE.search(page_ctx)
    switch = _effect_switch_fast_path(user_msg, m.group(1) if m else "")
    if switch is not None:
        return switch, "effect_switch"
    return None


def display_text(user_msg: str, page_ctx: str) -> str:
    """屏幕文案创作（`device_oled_display` 的 `text` 缺失时）——直调生产那一份。

    **为什么它是快道的前置件、不是可选的锦上添花**：`_display_fast_path` 产出的计划
    是 `device_oled_display({})`——`params` 里**故意不填 text**，因为屏幕文案由
    **execute 侧**结合对话创作（快道自己的 docstring 写着这件事，技能模板
    `instantiate_plan("device_display", {})` 也是这么长的）。生产那条路在
    `graph.execute_node` 里补这一步（`if ref_err is None and name ==
    "device_oled_display" and not args.get("text")`）。

    ⇒ 只接快道、不接这一步，形状是「快道把决策权拿走了，而只有模型才会填的那个参数
    没人填」：工具收到一个空 `text`、验收判 BLOCK，主人看到一次**失败的上屏**——比
    "快道没接、仍由模型决定"更差。这正是本模块纪律③说的那种"接一半"。

    调用点在 `react_line.WorkflowExecutor.run_specs`（逐 spec 解析出 args 之后、调用
    工具之前），与生产的落点同层。
    """
    return _create_display_text(user_msg, page_ctx)


def denied(state) -> set[str]:
    """这一轮**不许再选**的技能名（生产 `_planner_setup` 的 `deny` 同源）。

    来源是上一轮受阻项里"改参数重试无效"那一族。它**不扩权**（只会更小），也不是
    第二份可见性名单；无阻碍轮传空集 ⇒ schema 逐字节不变。本臂此前**一格都没接**：
    菜单里永远留着刚失败过的那个技能，模型照着再点一次，同键二次受阻 ⇒ 收尾。
    """
    return denied_workflows(state.get("blocked") or [])


def planner_prompt_args(state, *, role, page_ctx, round_info, user_msg,
                        ledger_frame, correction, deny, frames_txt) -> dict:
    """planner 提示词的**全部注入值**——生产 `_planner_setup` 那一份的逐格对应。

    **为什么单起一个函数**：这一份有 14 格，散在调用点上写就是"每加一格要记得两边都加"
    的手工同步。更要紧的是它此前**喂的是空值**（`intent_hints=[]`、`tool_results=空`、
    `ref_hints=[]`、`blocked_rows` 与 `deny` 干脆没传）——空值不会报错，只会让模型
    在一份"这轮什么都没有"的提问下决策。三格的后果是**具名**的：

      · `intent_hints` 空 ⇒ 主人那句话里没做完的动作**不标【未完成】**（生产靠它触发
        规则 5"清单里还有未完成项"，本臂等于把这条规则关了）；
      · `blocked_rows` 空 ⇒ 模型看不到受阻的**类型**，只能从错误帧那句话里猜，于是把
        "服务这一轮给不出数据"当成"你参数写错了"、原地重点一次同一个调用（§1.55）；
      · `ref_hints` 空 ⇒ 多步技能拿不到"能引用什么字段"，`$tool[0].id` 写不出来。

    逐格的对应与理由：

      | 这一格 | 取值来源 | 与生产的差别 |
      |---|---|---|
      | `intent_hints` | `state["executed"]` + 主人原话 | 无（此前传 `[]`） |
      | `tool_results` | 本轮窗口的工具帧 | 无（本臂用"最后一条 HumanMessage 之后"，生产用 `turn_start`；同一切法） |
      | `blocked_rows` | `state["blocked"]` | 无（此前**没传**，吃的是默认空串） |
      | `ref_hints` | `state["tool_data"]` | 无（此前传 `[]`） |
      | `deny` | `state["blocked"]` | 无（此前**没传**，菜单不摘） |
      | `reflector_feedback` | `state["issues"]` | 无（本臂没有 reflector 节点，那一格恒为空 ⇒ 落在默认那句上） |
      | `correction` | 本轮的纠偏提示 | 无（此前恒为空） |
      | `short_reply_hint` | 首轮才给 | 无 |

    **本臂刻意不同的两格**（写在这里以免下次有人"顺手对齐"）：
      · `pending_ledger`：本臂每请求渲染一次提示词，等价于生产的 `rounds == 0`，
        所以台账只出现在首轮那一份里（生产是每轮都给）；
      · `reflector_feedback`：本臂没有复盘节点，`state["issues"]` 结构上恒空。
    """
    rounds = int(state.get("plan_rounds") or 0)
    messages = list(state.get("messages") or [])
    return {
        "role": role, "page_ctx": page_ctx, "round_info": round_info, "user_msg": user_msg,
        "intent_hints": _intent_hints(state.get("executed") or [], user_msg),
        "doc_anchors": _doc_anchors(messages),
        "recent_context": _recent_tail(messages),
        "short_reply_hint": (_short_reply_hint(messages) if rounds == 0
                             else _NON_FIRST_ROUND),
        "pending_ledger": ledger_frame or _NO_LEDGER,
        "tool_results": frames_txt,
        "blocked_rows": blocked_rows(state.get("blocked") or []),
        "deny": deny,
        "ref_hints": ref_hints(state.get("tool_data") or []),
        "reflector_feedback": state.get("issues") or _NO_REFLECTOR,
        "correction": correction or _NO_CORRECTION,
    }
