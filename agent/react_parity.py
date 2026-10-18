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
from agent.graph import (_DecideCtx, _auto_task_frames, _confirm_grant_plan,
                         _create_display_text, _decide_register,
                         _decide_write_guard, _drop_correction, _evidence_on,
                         _fastpath_on, _finalize_dedupe, _finalize_unaccounted,
                         _narrator_system_pair, _narrator_wire)
from agent.planner_llm import PlannerDecision, _split_pseudo_calls
from agent.prompt_context import (REPLY_KIND_ARG, _doc_anchors, _has_frames,
                                  _last_assistant_utterance, _recent_tail,
                                  _short_reply_hint, blocked_rows,
                                  parse_reply_kind)
from agent.provenance import EVIDENCE_ARG, parse_evidence
from agent.tasks import INTENTS_ARG, normalize_intents
from agent.workflow_defs import visible_workflows

logger = logging.getLogger(__name__)

# 确认轮受阻那一支的注记（措辞与生产 `graph._planner_preflight` 那一格逐字相同）。
#
# **为什么抬成模块级常量、不直接写在字典字面量里**：`eval/wordlist_gate.py` 的穷举量
# 那一层数的是"中文串住在集合字面量里"的数量——一条**措辞**进 dict 也会被计成"新增
# 词表"。它确实不是词表，但把它抬出来比给闸门加豁免诚实：豁免是判据替自己打的补丁，
# 每加一处就少看见一块（那条闸的头注写着这句话）。
_CONFIRM_WRAP_REASON = "确认轮执行受阻，不重发清单"

# 「这一轮已经纠偏过一次」在本臂的**编码**（见 §G `register` 的长注）：生产把这件事
# 存在 `_DecideCtx.correction` 这个字符串里（非空 = 纠过），本臂存在 `ctx["corrected"]`
# 这个布尔里，而 `ctx["correction"]` 喂过提示词就清空了 ⇒ 透传会让登记链每轮都纠。
# 抬成模块级常量而不是写在调用点上，理由同 `_CONFIRM_WRAP_REASON`（`eval/wordlist_gate.py`
# 数的是"中文串住在集合字面量里"的数量）。
_CORRECTED_LATCH = "（本轮已纠偏一次）"


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


def decide_ctx_kwargs(*, state, config, rounds: int, user_msg: str, role: str,
                      page_ctx: str, principal, plan_obj: dict,
                      has_frames: bool, decided, correction: str = "") -> dict:
    """合成一份 `graph._DecideCtx` 的入参（**逐格给，一个默认值都不省**）。

    这张字典就是"本臂喂给生产那条链的全部输入"，所以它同时是**锁的对象**：
    `tests/test_react_write_guard_ctx.py` 拿 `dataclasses.fields(_DecideCtx)` 的名字集合
    与这里的键集合对差集。生产那个 dataclass 加一格（尤其**带默认值**的那一格——
    不带默认值的会在构造时当场 TypeError，响亮是白送的）而这里没跟上 ⇒ 那条锁红。

    逐格的取值理由（只列**不是"本臂没有、给空"**的那些）：

      · `state` / `config` / `principal` / `user_msg` / `role` / `rounds` / `plan_obj`
        —— 拒绝链真正读的七格，全部来自本臂的 ctx（同一个请求的同一份值）。
      · `has_frames` —— **由调用方算好传进来**，不在本模块现算：本臂的"本轮有没有帧"
        读的是"最后一条 HumanMessage 之后"（`react_arm._turn_tail`），与生产的
        `_turn(state)`（冻结 `turn_start`）是两套坐标。把切法搬进本模块等于让同一条
        判据有两个产地。
      · `decided` —— 决策模型那条原始响应。今天只有影子那一段读它
        （`getattr(c.decided, "evidence", ())`），而本臂的 schema 里没有 `evidence` 格
        ⇒ 结构上恒空。仍然照实传：**"这一格恒空"与"这一格没接"在 trace 上不是一回事**。
      · `_evidence` —— 读**唯一读口** `graph._evidence_on()`。写死 `False` 会让
        "档位开着"与"这一臂没接"长得一样。
      · `correction` —— **由调用方按段给**（§G 的登记链把它当"这一轮纠过没有"读，
        §D 的拒绝链一格都不读）。默认空串是 §D 那一侧的中性值；登记链的编码见
        `register` 的长注（本臂的"纠过没有"住在 `ctx["corrected"]`，不是这个串里）。
      · 其余（`task_sink` / `doc_anchors` / `ledger_frame` / `round_info` / `frames_txt`
        / `gate_note` / `deny` / `_base_llm` / `llm` / `deny_pseudo` /
        `correction_kind` / `planner_note` / `raw` / `workflow_name` / `params` /
        `_tools_off` / `_img_turn` / `llm_attempt`）—— 这一条链**一格都不读**（逐格核过），
        给的是它们各自的中性值。给空不等于判据被削弱：读不到的格影响不了判决。
    """
    return {
        "state": state, "config": config, "task_sink": None,
        "rounds": rounds, "user_msg": user_msg, "role": role,
        "page_ctx": page_ctx, "has_frames": has_frames, "doc_anchors": (),
        "ledger_frame": "", "principal": principal, "round_info": "",
        "frames_txt": "", "gate_note": "", "deny": frozenset(),
        "_base_llm": None, "_evidence": _evidence_on(), "llm": None,
        "deny_pseudo": set(), "correction": correction, "correction_kind": "",
        "planner_note": "", "plan_obj": plan_obj, "raw": None,
        "decided": decided, "workflow_name": "", "params": {},
        "_tools_off": False, "_img_turn": False, "llm_attempt": 0,
    }


def write_guard(state, *, ctx, plan_obj: dict, rounds: int,
                has_frames: bool) -> dict | None:
    """写目标拒绝链 → 命中时返回**被就地改写过的 plan_obj**；没命中 ⇒ None。

    直调 `graph._decide_write_guard`（**整条**，不是按顺序逐个调那九个函数）。

    **为什么整条直调**：那九个的**顺序是承重的**——引号片段校正 → 公告文本 → 目标名 →
    名字值 → 待办正文 → 目标出处 → 目标 → 台账编号 → 政策，每一对的先后都有各自的
    现场理由（写在 `_decide_write_guard` 的长注里）。按顺序抄一遍等于把那份顺序养成
    第二份，而"顺序错了"的形状是**换一条理由拒绝**：两边都拒绝、都零写、都如实说，
    只是说的原因不同——那种差在报告里看不出来，只有把两边放在一起读才看得见。

    **返回的是 `plan_obj` 而不是生产那个 `_Step.ret`**：`_Step.ret` 是
    `graph._plan_update` 的产物（生产的 state 形状），而本臂的 state 由自己的
    `_plan_update` 写。判据（"拒绝了没有"）相同、**产物形状不同**——这正是"适配"
    与"抄判据"的分界，同 `plan_has_tools`。

    ⚠️ 命中时 `c.plan_obj` 已经被换成了 `_wrap_up_plan`（带那句"这件事这次没有做：
    站内数据一个字节都没有改动"）并挂上 `refusal` 那一格。调用方**必须用返回的这份**，
    拿原来那份等于把拒绝丢了——而拒绝丢了之后这一轮会照常执行那件写。
    """
    kw = decide_ctx_kwargs(state=state, config=ctx.get("config"), rounds=rounds,
                           user_msg=ctx.get("user_msg") or "",
                           role=ctx.get("role") or "",
                           page_ctx=ctx.get("page_ctx") or "",
                           principal=ctx.get("principal"), plan_obj=plan_obj,
                           has_frames=has_frames, decided=ctx.get("draft"))
    c = _DecideCtx(**kw)
    step = _decide_write_guard(c)
    if step.action == "next":
        return None
    # 生产那条链只有两个出口（"next" / "return"，见 `_decide_write_guard` 的两处
    # `return _Step(...)`）。第三种动作意味着它被改过了，而这里**没有对应的接线**
    # ⇒ 响亮地炸，不猜。
    if step.action != "return":
        raise RuntimeError(f"_decide_write_guard 返回了没接线的动作 {step.action!r}")
    return c.plan_obj


def finalize_dedupe(state, *, ctx, plan_obj: dict, rounds: int, has_frames: bool,
                    turn_messages: list) -> tuple[dict, bool]:
    """`graph._finalize_dedupe`（字面路径修正 + 八条去重/拦截守卫）
    → `(定稿后的 plan_obj, 这一轮到此为止)`。

    ⚠️ **state 视图必须换成"本轮窗口"**：那个函数内部读 `_turn(state)`——生产 state 里
    有冻结的 `turn_start`，本臂没有那个坐标。整条 `messages` 传进去会把 **DB 历史里的
    帧**一起算进来，于是"这个动作本回合已经做过了"在一次都没做过时也成立（它要防的
    正是相反面：重复执行同一个动作）。窗口由调用方算好传进来（`react_arm._turn_tail`），
    切法不在本模块现算——同 `decide_ctx_kwargs` 的 `has_frames`。

    其余各格（`receipts` / `executed` / `noop_specs` / `blocked` / `pending_confirm` …）
    照原样传本臂的 state：它们本来就是同一份 `AgentState` 形状，且都是**请求内累计**
    的（本臂由 `execute` 回写）。

    第二个返回值是"这一轮到此为止"：生产那八条守卫命中时都 `return plan_obj,
    _plan_update(plan_obj, rounds)`，调用方直接返回、不再走收尾。本模块**不转发那个
    update**（它是 `graph._plan_update` 的产物、生产的 state 形状），只转发那个事实，
    由本臂自己的 `_plan_update` 造返回值——同 `write_guard`。
    """
    out, update = _finalize_dedupe(plan_obj, {**state, "messages": list(turn_messages)},
                                   ctx.get("user_msg") or "", rounds, has_frames)
    return out, update is not None


# ══════════════════════════════════════════════════════════════════════════════
# §E 剔空纠偏：点名的工具一个都没执行 ⇒ 纠偏一次，第二次确定性如实收尾
# ══════════════════════════════════════════════════════════════════════════════
def drop_correction(dropped: list, role: str | None) -> str:
    """剔空纠偏文本——直调生产 `graph._drop_correction`（零 LLM）。

    现场（20260921 22:34，见那个函数的 docstring）：管理员问某篇草稿的标签，planner
    点名 `list_admin_notes` **意图对路**，但那工具属于 `admin_notes` 技能、不在
    content_query 的白名单里 ⇒ 清单被剔空 ⇒ 旧行为当收尾轮处理 ⇒ narrator 零帧编话
    ⇒ gate 打回 ⇒ 主人看到一句降级回复且**这一轮直接结束**（原话："居然就直接结束
    而不是重新规划执行"）。

    本臂此前**一格都没接**：`instantiate_plan` 早就把 `dropped` 记在计划对象上了
    （`_plan_of` 拿得到），而没有人读它——形状是"点名全被剔除 ⇒ 零工具 ⇒ 交给
    narrator"，与上面那个现场逐字同形。

    `role` 只透传（工具归属与可见性判据都在生产那一侧，唯一读口 `visible_workflows`）。
    """
    return _drop_correction(list(dropped or []), role)


def unaccounted(plan_obj: dict, rounds: int, has_frames: bool) -> dict | None:
    """`graph._finalize_unaccounted`（三格确定性终局）→ 定稿后的 `plan_obj`；
    **三格都不命中 ⇒ None**。

    三格 = 剔空后仍零工具 / 参数不齐后仍零工具 / 记账字段全空的零工具轮。它们的共同
    点是"这一轮一个工具都不会执行，而系统判得出来该有一条如实的收尾注记"——**绝不把
    "零工具零帧"直接交给 narrator**（那正是 22:34 那一轮的形态：它只能编）。

    本臂此前也没有这一格：`_plan_of` 判出零工具之后直接去 `model`，于是上面三种形态
    全靠 narrator 自己不说错话。而这三条注记里写的是**禁止句**（"不许出现「看过/
    读过/查过/调用过工具」"），生产那侧反复吃过"机制描述会变成 narrator 的词汇"的亏。

    返回 `plan_obj` 而不是生产那个 `_plan_update` 产物：同 `write_guard`（判据相同、
    产物形状不同）。
    """
    upd = _finalize_unaccounted(plan_obj, rounds, has_frames)
    return None if upd is None else upd.get("plan_obj")


# ══════════════════════════════════════════════════════════════════════════════
# §G 任务台账登记：三个伪函数 + 意图清单 → `__TASK__` 帧
# ══════════════════════════════════════════════════════════════════════════════
# 这一族的**病**（本臂此前一格都没接）：三个任务伪函数（`task_hold` / `task_drop` /
# `task_intents`）**常绑在菜单里**（`build_tool_schema`，20261015 撤档后对所有角色可见），
# 模型完全发得出来；而本臂的 `_plan_of` 拿技能名去 `instantiate_plan`，那三个名字**不在
# `WORKFLOW_MAP` 里** ⇒ `WORKFLOW_MAP.get(name) or WORKFLOW_MAP["chat"]` 静默回退成
# `chat`、零工具、零注记、零 trace。实测（离线直调 `instantiate_plan`）：
#
#     task_hold -> chat [] ''      task_drop -> chat [] ''      task_intents -> chat [] ''
#
# 形状是"模型说「这件事我记下了/这件事不做了」，系统把它读成闲聊"。而**没有任何东西会红**
# ——`require_task_state` 那一族在本臂上从来没被满足过，报告里只是一条稳定的红。
#
# 同族的第二个出口是**挂在每个技能上的 `intents` 字段**（`tasks.intents_prop_schema`）：
# 本臂此前也没摘 ⇒ 模型枚举的清单被当成一个技能参数送进 `instantiate_plan`，换来一条
# `param_unknown` 告警 + 清单一个字节都不剩。
#
# 接法：**不重写判据**——伪函数拆分直调 `planner_llm._split_pseudo_calls`（生产
# `tool_calls_to_plan` 用的同一个），登记直调 `graph._decide_register`，自动登记直调
# `graph._auto_task_frames`。本模块只做三件适配：把本臂的 state 喂成它们认的形状、
# 把"只纠偏一次"那格译成生产读的那一格、把三格非技能参数摘走。
#
# **`_split_pseudo_calls` 是私有名**，仍然直调：它是生产那条路的**唯一拆分点**，
# 抄一份进本模块就等于把"哪几个名字是伪函数"养成两份，而 `TASK_HOLD` 这些常量在
# `tasks.py` 里改名时**两份不会一起动**（本仓最贵的那类事故）。宁可 import 一个私有名。


def split_pseudo_calls(calls: list, role: str | None) -> tuple[list, dict | None,
                                                               tuple, list[str]]:
    """三个伪函数从调用里摘出来 → `(剩下的调用, declare, intents, notes)`。

    直调 `planner_llm._split_pseudo_calls`（生产 `tool_calls_to_plan` 的第 2 步）。
    `notes` 是**机器可读的异常记账**（`task_drop_ignored` / `task_hold_invalid` /
    `task_intents_invalid`）——生产把它塞进 `PlannerDecision.notes` 进 trace，本臂
    由调用方**响亮地**记一笔（见 `react_arm.planner_node` ⑥a）。
    """
    return _split_pseudo_calls(list(calls or []), role)


def strip_injected_args(calls: list, role: str | None,
                        intents: tuple = ()) -> tuple[list, tuple]:
    """把**不是技能参数**的那三格从 args 里摘走 → `(调用, 意图清单)`。

    `intents` / `reply_kind` / `evidence` 由 `build_tool_schema` 挂在**每个**技能上
    （前者无条件、后者按 `AGENT_EVIDENCE` 射程），却都不是那个技能的参数。生产
    `tool_calls_to_plan` 在取到第一条调用后把三者 `pop` 走（那边的注写着"摘掉才是
    「这一格不存在」那条路径逐字节相同"）。本臂此前**没摘** ⇒ 模型填的 `intents`
    落进 `params`，换来一条 `param_unknown` 告警，而清单本身**一个字节都没留下**。

    ⚠️ **与生产唯一的口径差**：生产只摘**第一条**调用（`parallel_tool_calls=False`
    保证它那边恒为一条），本臂一轮可能有多条（见模块头注）⇒ 逐条摘、意图按原序合并。
    摘的**名字集合**与**归一函数**逐字同源（常量 import 自 `tasks` / `prompt_context`
    / `provenance`），所以改名会一起动。`tests/test_react_task_ledger.py` 拿
    `tool_calls_to_plan` 的源码做**机械对差**：它 `pop` 哪几个名字，这里就得摘哪几个。

    `reply_kind` 与 `evidence` 的归一产物在这里**丢弃**（生产把它们放进 `PlannerDecision`
    进 trace）：本臂的节点级 trace 事件族是**已登记的缺口**（见 `react_arm` 模块头注的
    "这一版仍然不做的"），`reply_kind` 那一格只进 trace、**不据它放行或拦截任何事**，
    所以丢弃不改这一轮的行为。照实写在这里，免得下次读的人以为接全了。
    """
    out: list[dict] = []
    inline: list[dict] = []
    for c in calls:
        args = (c or {}).get("args") if isinstance(c, dict) else None
        if not isinstance(args, dict):
            out.append(c)
            continue
        args = dict(args)
        parse_evidence(args.pop(EVIDENCE_ARG, None))
        parse_reply_kind(args.pop(REPLY_KIND_ARG, None))
        if INTENTS_ARG in args:
            inline.extend(normalize_intents({"intents": args.pop(INTENTS_ARG)}, role))
        out.append({**c, "args": args})
    return out, tuple(intents) + tuple(inline)


def unknown_workflows(calls: list, role: str | None) -> list[str]:
    """点名的技能里**不在本轮可见清单**的那些（生产 `tool_calls_to_plan` 同一条判据：
    `name not in {s.name for s in visible_workflows(role)}`）。

    ⚠️ 判据读的是 `visible_workflows`，**不是** `WORKFLOW_MAP`：两者差在
    `include_system` 的那几件（内部技能不在菜单里，模型点了也认不出）。照生产写，
    免得"本臂比生产严一点"变成一处说不清的差。

    调用方拿它做**响亮**的那件事（生产是 `tool_calls_to_plan` 返回 None ⇒ 确定性收尾）：
    本臂此前让这些名字落进 `_plan_of` → `instantiate_plan` 静默回退成 `chat`
    ⇒ "模型点了个菜单里没有的函数"与"模型这轮决定闲聊"长得一模一样。
    """
    ok = {w.name for w in visible_workflows(role)}
    return [str((c or {}).get("name") or "") for c in calls
            if str((c or {}).get("name") or "") not in ok]


def task_decision(declare: dict | None, intents: tuple, workflow: str,
                  params: dict | None = None) -> PlannerDecision:
    """本臂这一轮的决策对象——**用生产那个类型**（`planner_llm.PlannerDecision`）。

    为什么不自己搭一个 shim：`_decide_register` / `_auto_task_frames` / `_acted_workflows`
    读的是它的 `declare` / `intents` / `workflow` 三格，而生产将来给这个 dataclass
    加一格（带默认值的那种）时，自己那份 shim **不会跟着长**——症状是"那一格恒空"，
    正是本模块纪律③要消灭的静默。用同一个类型，加格要么有默认值（无害）、要么构造时
    当场 TypeError（响亮）。

    `workflow` 取**第一条真调用**的技能名（生产的 `PlannerDecision.workflow` 就是
    `calls[0].name`），纯登记轮没有真调用 ⇒ `chat`（生产 `tool_calls_to_plan` 那条
    `if not calls:` 出口逐字同款）。`_acted_workflows` 把 `""` 与 `chat` 都排除在外，
    所以这个取值决定的是"本轮派下去的那件算不算办过了"。
    """
    return PlannerDecision(workflow=workflow, params=dict(params or {}),
                           declare=declare, intents=tuple(intents or ()))


def auto_task_frames(state, config, decided, rounds: int) -> list[dict]:
    """意图清单 − 本轮办了的 ⇒ 自动登记的 `__TASK__` 载荷。直调 `graph._auto_task_frames`。

    它自己判"清单为空 ⇒ 一个字节都不产生、也不记 trace"，所以调用方不必先判。
    取现场事实的那几处（`_acted_workflows` / `_task_goal_sources` / `receipts`）都在
    生产那一侧，本模块一个字都不重写。
    """
    return _auto_task_frames(state, config, decided, rounds)


def register(state, *, ctx, plan_obj: dict, rounds: int, has_frames: bool,
             decided) -> tuple[dict, str, str, list | None]:
    """任务登记（生产 `graph._decide_register`）→
    `(plan_obj, action, correction, task_frames)`。

    `action` 就是生产 `_Step.action` 的三格：`"next"` 往下走 / `"continue"` 纠偏重决策
    **一次** / `"return"` 本轮到此收尾（`plan_obj` 已被换成带 `TASK_DONE_NOTE` 或
    `declaration_note` 的收尾计划，**必须用返回的这份**——拿原来那份等于把登记丢了，
    那一轮会照常去执行模型点的技能，而主人刚被告知"这件事我记下了"）。

    ⚠️ **"只纠偏一次"那一格要译**：生产读的是 `c.correction`（一个**跨阶段活着的字符串**，
    `_planner_setup` 建一次、`_decide_register` 只判真假）。本臂把"这一轮纠过没有"
    放在 `ctx["corrected"]`（一个布尔，与 ④ 的意图纠偏**共用**——生产也是同一个槽），
    而 `ctx["correction"]` 在喂过提示词之后就被清空（见 `react_arm.planner_node` ③ 的注）。
    ⇒ 直接透传会让 `_decide_register` 每次都读到空串、**每轮都纠**，而纠偏出口返回的是
    `{}`（不吃轮数）⇒ 死循环。所以这里按 `ctx["corrected"]` 补一个非空哨兵：它在这条链上
    只被当**真假**读（`not c.correction` 与 `record(..., corrected=bool(...))` 两处），
    补哨兵之后 `corrected=True` 记的也正是事实。
    """
    latch = (ctx.get("correction") or
             (_CORRECTED_LATCH if ctx.get("corrected") else ""))
    kw = decide_ctx_kwargs(state=state, config=ctx.get("config"), rounds=rounds,
                           user_msg=ctx.get("user_msg") or "",
                           role=ctx.get("role") or "",
                           page_ctx=ctx.get("page_ctx") or "",
                           principal=ctx.get("principal"), plan_obj=plan_obj,
                           has_frames=has_frames, decided=decided, correction=latch)
    c = _DecideCtx(**kw)
    step = _decide_register(c)
    if step.action == "return":
        # 第四个返回值是生产那个 `_plan_update(..., [frame] if frame else [])` 里的
        # **显式登记帧**（`frame_payload` 的产物，落库那一帧）。必须原样带出去：
        # 丢了它的形状是"计划里写着"这件事记下了"、而 `__TASK__` 帧一个都不发"，
        # 下一轮 planner 靠那个帧认人 ⇒ 同一件事被登记第二遍。
        # `is not None` 的语义照 `graph._plan_update`（`[]` 与 `[frame]` 都是显式传的）。
        return c.plan_obj, "return", "", (step.ret or {}).get("task_frames")
    if step.action == "continue":
        return plan_obj, "continue", c.correction, None
    if step.action == "next":
        return plan_obj, "next", "", None
    # 生产那三段只有这三个出口（见 `_decide_register` 的三处 `return _Step(...)`）。
    # 第四种动作意味着它被改过了，而这里**没有对应的接线** ⇒ 响亮地炸，不猜。
    raise RuntimeError(f"_decide_register 返回了没接线的动作 {step.action!r}")


# ══════════════════════════════════════════════════════════════════════════════
# §H narrator 的出站装配：**前缀缓存的两处切分**（稳定头 / 变化段 / 上下文块挪尾）
# ══════════════════════════════════════════════════════════════════════════════
def narrator_system_pair(state, config, role, receipts: list, block: str, drop: set,
                         has_action: bool) -> tuple:
    """narrator 的**两条**系统消息（稳定头 / 变化段）——直调生产 `_narrator_system_pair`。

    **为什么这一格必须接**（20261016 生产实测，486 次 narrator 调用）：前缀缓存命中的
    是**最长公共前缀**。此前那条系统提示词是一整条、里面从 `{plan}` 起每轮都不同 ⇒
    **它后面的一切（包括整整 20 条对话历史）一条都进不了缓存**：实测稳定头 8304 字
    ≈ 5033 tok，而每次命中的中位数**恒为 5248 tok**——命中的恰好只有那个头。

    本臂此前是单条 `_EXECUTOR_PROMPT`，形状与生产改之前一模一样 ⇒ 这次对照的**成本
    读数**（`eval/token_cost_report.py` 的前缀缓存命中率）两臂不可比。而成本是这次
    对照的一半论据（见计划），所以它不是锦上添花。

    逐格取值与生产 `model_node` 同源：`plan` 走 `_narrator_plan(state, config)`、
    `tool_frames` 走 `article_pointer=True`（生产只有 narrator 这一处这么用）、
    `fact_block` 的占位分岔按**回执**判（不是"印了几行"）。
    """
    return _narrator_system_pair(state, config, role, receipts, block, drop, has_action)


def narrator_wire(messages: list) -> list:
    """narrator 的出站序列：补形状 + 把**首条 `[System: …]` 上下文块挪到最后**。

    那条上下文块由 `server._build_messages` 生成，里面的 `current_time` 精确到分钟、
    `page`/`title` 随访客翻页就变 ⇒ 它站在第一位时，它后面的每一条消息都不可能是公共
    前缀的一部分。挪到最后，公共前缀就从"稳定头"延伸成"稳定头 + 整段历史"。
    **只动发给模型的这份副本**：`state["messages"]` 一个字不改。
    """
    return _narrator_wire(list(messages or []))


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
