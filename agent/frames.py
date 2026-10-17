# -*- coding: utf-8 -*-
"""帧契约：第二条臂（`agent/react_arm.py`）与生产（`graph.py` / `server.py`）之间的唯一事实源。

**为什么有这个模块**（20261016，`docs/adr/adr-0008-*`）。`agent/react_arm.py` 是**第二份
实现**：它产出**与生产逐字相同的帧**。两套实现共享一份契约，而契约此前**只活在约定里**
——三处症状，全都在这个仓里付过代价：

  ① **消费面靠 grep 数出来**。`gate_node` 从 state 读哪些键，是用 `awk` 切出函数体逐行
     grep 数的，抄成一张七格的表贴在 `_round_state` 的 docstring 里，另外三格（它调用的
     **辅助函数**读的）靠人补记。**抄漏不报错**——`.get("X", [])` 把"本线没喂"与"本轮
     确实没有"读成同一件事，闸门只是**少开火**。20261016 用 AST 走完整传递闭包实测：
     那张表少了一格（`task_frames`），`_popup_state` 那张四格的表少了**七格**。
  ② **判据分家且被锁死**。`react_line._consent_missing` 停在 `not self.grant`，而生产
     20261012 已按 `_grant_covers(state, spec)` 收窄两档（那两档各有一次生产实证：
     一次点头授权了两件写、同一件写静默落库三份）。更坏的是
     `tests/test_react_arm.py` ⑥d **把那一行字符串钉住了**——锁的意图是对的（"别另立
     一套判据"），锁的落点错了（钉了字面而非判据），于是漂移被**永久化**。
  ③ **同一件事各写一份**：`spec` 串的构造、`blocked` 行、收尾正文的措辞，散在两侧。

本模块把这一层**写下来并交给机械锁**（`tests/test_frame_contract.py`）：键集从 `graph.py`
现取、与这里的声明算**双向**差集（多一个红、少一个红），并带反向对照。规矩与本仓既有的
`tests/test_state_keys.py` 同族——**手抄的表必须由机器守，且守的必须是取值本身**。

**批 2（20261016）之后本模块瘦了一圈，判据反而更强**：臂不再手搓 `updates` 二元组、不再
给分片冒充 `langgraph_node`、不再合成 state —— 它是一张真的 `StateGraph`
（`docs/adr/adr-0009-*`），`planner`/`execute`/`model`/`gate` 是编译进图里的**真节点**，
那两个生产资产拿到的是**真 state**。于是本模块从"手搓时该填什么"的说明书，变成
**"结构上必须成立什么"**的判据来源：

  · `NODE_FRAMES` —— 编译出来的图的节点集必须 ⊇ 这四个名字（producer 按名取键，
    多一个名字无害、少一个是那一格**静默丢掉**）；
  · `VISIBLE_NODE` —— 用户可见正文挂的节点名（`run_golden` 只把这一格拼进 `text`）；
  · `GATE_STATE_KEYS` / `POPUP_STATE_KEYS` —— 那两个资产的**读侧闭包**必须全是
    `AgentState` 的**声明键**（未声明的键被 langgraph 静默丢掉）；
  · `spec_text` / `blocked_row` / `tool_frame` / `plan_for_gate` —— 唯一构造点。

**契约的三层，本模块只管中间那一层**：
  · `AgentState` 的声明键（`graph.py`）—— 图内部的账，`tests/test_state_keys.py` 守；
  · **producer 读的 `updates` 键**（`model`/`planner`/`execute`/`gate`）与**它读那些节点
    返回的哪些键** —— 这就是本模块（`NODE_FRAMES` / `*_STATE_KEYS`）；
  · 连线前缀（`__CMD__` / `__CONFIRM__` / `__PENDING__` / …）—— 那是 `server.py` 的事，
    臂**不产连线帧**（它产 `updates`，producer 翻译），所以不在本模块射程内。

**一条纪律**：本模块的函数**只做形状**，不做判断。任何"这一轮该不该弹卡""这句话算不算
声称"的判据都必须留在 `graph.py` 里由两边**直调**（`_confirm_popup` / `gate_node` 就是这么
接的）——抄第二份判据就是抄一条漂移源，而"人工同步 94 处"正是本仓架构审计的头号结论。
"""
from __future__ import annotations

import json
import logging
from typing import Any

from langchain_core.messages import ToolMessage

logger = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════════════════════
# 契约声明（下面这些常量是**声明**，不是缓存——`test_frame_contract.py` 从源码现取比对）
# ══════════════════════════════════════════════════════════════════════════════

# `server._run_agent_stream_to_queue` 按名字消费 `updates` 里**恰好这四个**节点。
# 臂产任何别的名字 = 那一格被生产者**静默丢掉**（它按名取键，取不到就不发对应的帧），
# 而报告里看不出是"臂没产"还是"生产者没认"。
# **批 2 起这条是结构判据**：臂是一张真图，这四个名字必须**编译在图里**
# （`test_frame_contract.py` 拿 `get_graph().nodes` 与它比对）。
NODE_FRAMES = ("planner", "execute", "model", "gate")

# `messages` 通道上**用户可见正文**挂的节点名。生产那条是 `model_node`（零工具 narrator）；
# 臂这一格**也是真节点**（`langgraph_node` 由 langgraph 自己盖，不再有人手填）。
# `run_golden.run_one` 只把 `langgraph_node == "model"` 的分片拼进 `text`，
# 改名等于每一条文本断言都被中间推理污染。
VISIBLE_NODE = "model"

# ── `gate_node` 的读侧闭包（含它调用的辅助函数）──────────────────────────────
# 这**不是**"它函数体里 grep 到的那七个"——那七个只是直接读的；下面另四个是辅助函数读的
# （`gate_node` 函数体里看不见，20261016 用 AST 走传递闭包实测）。逐条标出来源：
#
#   messages         gate_node:13292   —— 判"有没有执行事实"的唯一依据（它从消息流挑 ToolMessage）
#   plan_obj         ← plan_view:329  —— 分族判据的入参（**对象那一态**，20261017 批 1）
#   gate_replan      gate_node:13307   —— 上一圈有没有重规划过
#   ledger           gate_node:13366   —— 洞⑦ 的台账否认判据
#   pending_confirm  gate_node:13367
#   noop_specs       gate_node:13369
#   receipts         gate_node:13611
#   blocked          ← _blocked_article_targets:13206（5a 混合轮收窄）与 _round_facts:3337
#   blocked_seen     ← _round_facts:3347（账甲的 `consent_blocked`，洞⑥ 确认话术声称）
#   confirm_grant    ← _round_facts:3349 与 _ledger_in_scope:3404
#   task_frames      ← _ledger_in_scope:3406（账乙网的语境判据）
#
# ⚠️ `task_frames` 是 20261016 补上的那一格。它缺席的形态**极难看出来**：`_ledger_in_scope`
# 的判据是"`confirm_grant` 在场 **或** 本轮 `task_frames` 非空"，少喂一格只是让那条
# **析取**少一条腿——闸门照常开火，只是账乙网在"这一轮 planner 刚登记过任务"的现场
# 静默不启用。臂今天**不产** `task_frames`（如实登记的缺口，批 3 补），所以这一格对它是恒空；
# 但**恒空与缺席不是一回事**：缺席是"没人喂"，恒空是"这一线确实没有任务"。写下来才分得清。
#
# **批 2 起这张表的判据换了个方向**：臂拿的是真 state，所以"喂没喂"不再是问题——
# 问题是"这张表里有没有 `AgentState` **没声明**的键"（未声明 ⇒ 被 langgraph 静默丢掉，
# 本仓为此付过 13 次代价）。判据 = `GATE_STATE_KEYS ⊆ AgentState.__annotations__`。
# ⚠️ `turn_start` 是 20261016 合并时被这条锁**当场抓出来**的一格：本支的读端窗口让
# `gate_node` 的闭包多读了它（`_turn` / `_narrator_msgs` 用它把"本轮"从整表里切出来），
# 而主线那份声明写在这之前。**两边各自都对、合起来才错**——正是这条双向锁存在的理由。
# 它也不是"多喂一格无害"：缺席时 `state.get` 落到 `-1` ⇒ 窗口恒等于整表 ⇒ 线程一复用，
# 闸门看到的就是整条会话（批 1 治的正是这一格）。
# ⚠️ 计划那一格 20261017 批 1 从 `plan` 换成 `plan_obj`：读侧不再读渲染出来的文本，改读
# 计划对象（`plan_view`）。**这一格不能两态都留**——留下 `plan` 就是"声明了没人读"，
# 而那正是这张表存在的理由（陈旧声明与漏登一样会让人读错这张表）。
GATE_STATE_KEYS = frozenset({
    "blocked", "blocked_seen", "confirm_grant", "gate_replan", "ledger", "messages",
    "noop_specs", "pending_confirm", "plan_obj", "receipts", "task_frames", "turn_start",
})

# ── `_confirm_popup` 的读侧闭包（含它调用的辅助函数）────────────────────────
# 弹卡那一格的形状判据（合成 state 的年代，docstring 写的是"它只读 state 的四个键"——
# 20261016 实测**是十一格**，那四个只是 `_confirm_popup` **函数体里**直接读的）：
#
#   messages         _confirm_popup:12113 —— 喂 `_target_evidence`（"文章 id 有据"）
#   plan_obj         ← _plan_workflow:12362 —— 令牌里签的技能名，空 ⇒ sign 拒签 ⇒ 卡弹不出
#   receipts         _confirm_popup:12260
#   ledger           _confirm_popup:12130（`_ledger_pending_text`）
#   executed         ← _grant_covers:8837 —— **20261012 第二次收窄那一格**
#   blocked / blocked_seen / noop_specs / pending_confirm  ← _round_facts:3319-3347
#   confirm_grant    ← _all_granted:8853
#
# ⚠️ `executed` 是这一格里**最要紧**的一格，也是 20261016 之前**根本没喂**的一格。
# `_grant_covers` 拿它实现"令牌用在那一件上就作废"（20261012 第二次收窄，现场
# `20261012T195727`：同一件写静默落库三份）。缺了它 ⇒ `state.get("executed") or ()` 空 ⇒
# 那一格判据**恒不生效** ⇒ "同一件第二次"又退回被令牌覆盖。
# **批 2 起这一格不再靠"喂"**：`execute` 节点把本轮真执行过的 spec 写回 `state["executed"]`
# （`executed_specs(executor.calls)`，与生产 `execute_node` 同一种累积），
# 弹卡拿到的是真 state，那一格由结构保证在场。
# ⚠️ 计划那一格同 `GATE_STATE_KEYS`（20261017 批 1）：`plan` 退场，只剩 `plan_obj`
# ——本格原先两态都在，是因为 `_plan_workflow` 早就直取对象、而 `_round_facts` 那时还在
# 读文本；现在两处都读对象，`plan` 就成了一条没人读的陈旧声明。
POPUP_STATE_KEYS = frozenset({
    "blocked", "blocked_seen", "confirm_grant", "executed", "ledger", "messages",
    "noop_specs", "pending_confirm", "plan_obj", "receipts", "turn_start",
})


# ── 逐字抄生产的中文串（**不是**"另一份文案"，是同一句）────────────────────────
# 生产在 `graph.py:12542`（零改动轮）也写这一句。两处各写一遍就是一条漂移源——用户读到的
# 是同一句话，改一处漏一处会让两条臂的读数差在**文案**上，而那正是评测最容易误读的差异。
# 本批不动 `graph.py`，所以这里做不到"收归一处"；**退而求其次：把它命名，让锁能比对**
# （`tests/test_frame_contract.py` ④：从 `graph.py` 源码现取那一句，与这里逐字相等）。
NOOP_TEXT = "这一步现在没有需要改动的地方，我就没有动手。"


# ══════════════════════════════════════════════════════════════════════════════
# spec 串：构造一处、解析走生产那两个函数
# ══════════════════════════════════════════════════════════════════════════════

def spec_text(tool: str, args: Any) -> str:
    """`(工具, 参数)` → spec 串 `<工具>(<json>)`——**本仓所有 spec 串的形态**。

    读侧永远是生产的 `graph._tool_args` / `_tool_name`（臂直接 import），所以漂移面只剩
    **构造**这一侧；而构造此前在 `react_arm._blocked_row`、`graph.py:8765`、
    `graph.py:3562` 各写了一遍。本模块收的是**臂这一侧**的唯一构造点
    （`graph.py` 那两处不动——那一半是生产路径，本批一个字节都不碰，见 ADR-0008 的
    《不做》）。`json.dumps` 的 `ensure_ascii=False` 与 `separators` 都必须与生产同款：
    读侧 `_tool_args` 先 `json.loads` 再 `ast.literal_eval`，空白与转义方式不影响解析，
    但**指纹**（`_spec_signature` 的 `str(v)[:200]`）比的是值不是串，所以这里只要
    能被解析回来即可——真正要逐字一致的是**回执**，那由 `tool_frame` 那一侧保证。
    """
    try:
        body = json.dumps(args or {}, ensure_ascii=False)
    except Exception:  # noqa: BLE001 —— 见 `blocked_row`：宁可给一个解析得出的空参
        logger.warning("[frames] spec 参数不可序列化，按空参落串：tool=%s", tool,
                       exc_info=True)
        body = "{}"
    return f"{tool or ''}({body})"


def executed_specs(calls: list) -> list[str]:
    """本轮**真执行过**的调用 → 生产的 `executed` 形态（spec 串列表）。

    `_grant_covers` 读它实现"令牌用在那一件上就作废"（见 `POPUP_STATE_KEYS` 的注）。
    生产那一格由 `execute_node` 累积（`executed + [s for s in specs if s not in executed]`，
    `graph.py:12868`），臂没有那份回写，所以从 `executor.calls` 现算——**只增不减**，
    与生产同一种累积。`_grant_covers` 自己对每一项做 `_spec_signature` 归一化，所以
    这里给原始形态即可（重复项也不会让判据出错，只是多跑几次循环）。
    """
    return [spec_text(str(c.get("tool") or ""), c.get("args"))
            for c in (calls or []) if isinstance(c, dict)]


# ══════════════════════════════════════════════════════════════════════════════
# 帧构造：只剩"工具帧"与"受阻行"（`updates` 的四类格子不再由本模块造）
# ══════════════════════════════════════════════════════════════════════════════
# **20261016 批 2**：`planner_frame` / `model_frame` / `gate_frame` / `execute_frame` /
# `visible_chunk` 五个构造器整体删除。它们存在的理由是"臂自己手搓 `updates` 二元组、
# 给分片冒充 `langgraph_node`"——而现在臂是一张真的 `StateGraph`，四类格子由**节点名**
# 产生（`planner`/`execute`/`model`/`gate` 是编译进图里的真节点），`langgraph_node` 由
# langgraph 自己盖。**手搓这件事在结构上不可能存在了**，构造器随之失去存在理由：
# 留着它，下一个人就能在图外面再手搓一份形状对的帧，而那正是这一批要消灭的东西。
#
# `NODE_FRAMES` / `VISIBLE_NODE` 两条声明**留着**：它们现在是"图里必须有哪些节点名"
# 的判据（`test_frame_contract.py` 拿编译出来的图的节点集与它比对），不再是"手搓时
# 该填什么名字"的说明书。


def tool_frame(i: int, c: dict) -> ToolMessage:
    """一次已发生调用 → 工具帧（`messages` 通道那一条 `ToolMessage`）。

    单独成函数只为**一处措辞**：文章读取快道要把它喂给模型（系统提示的 `tool_results`），
    `_on_tools` 要把它发给用户/报告——两处各自拼一份字符串，改一处漏一处就是一条新漂移源。

    `tool_call_id` 用 `react_{下标}`：golden 只读 `name` 与 `content`，不需要与任何
    `AIMessage` 的 `tool_calls` 对上，造一个稳定的即可。**同一个下标在两次调用之间
    可能撞名**（`execute` 每轮从 0 重数），但工具帧之间不互相配对，撞了也无害。
    """
    return ToolMessage(content=str(c.get("result") or ""),
                       name=str(c.get("tool") or ""),
                       tool_call_id=f"react_{i}")


def blocked_row(b: dict) -> dict:
    """受阻台账 → producer 读的那一行（它读 `spec` 与 `reason`）。

    `spec` 的形态与生产一致（`spec_text`），producer 用 `_spec_one` 解回来再渲染人话
    动作（`tool_action_text`）——自己拼中文动作行会让过程行两套措辞。
    """
    return {"spec": spec_text(str(b.get("tool") or ""), b.get("args")),
            "reason": b.get("reason"), "tool": b.get("tool")}


# ══════════════════════════════════════════════════════════════════════════════
# 计划塑形：`plan_obj` → 能被 `plan_state` 编码的那份
# ══════════════════════════════════════════════════════════════════════════════

def plan_for_gate(plan_obj: dict) -> dict:
    """`plan_obj` → **能被 `plan_state` 编码**的那份（缺键补上，不静默）。

    `plan_state`（= `plan_encode` 的唯一调用点，`tests/test_plan_channel.py` 用源码锁
    钉着）要 `workflow` 与 `reply` 两个键，缺一个就是 `KeyError`。本线的 `_plan_of` 在
    **一条工具调用都没发生**时返回 `{}`（纯闲聊轮），那时生产那边的技能名是 `chat`
    ——按它补，别填空串：`gate_node` 拿 `plan["workflow"]` 当分族判据的入参，空串会退化
    成宽查（漏判，方向安全但读数与生产不是同一档）。
    """
    obj = dict(plan_obj or {})
    obj.setdefault("workflow", "chat")
    obj.setdefault("tools", [])
    obj.setdefault("note", "")
    obj.setdefault("reply", "")
    return obj


# ── 批 2（20261016）：三份塑形器整体退场 ─────────────────────────────────────
# `popup_state` / `round_state` / `gate_state` 三个函数**删掉了**。它们此前的作用是
# 把臂自己攒的碎片拼成一份"最小 state"喂给生产那两个资产（`gate_node` /
# `_confirm_popup`）——而这一批之后臂是一张真图，那两个资产拿到的是**真 state**
# （`AgentState` 的实例，由 `graph_input` 给初值、由节点在请求内累积）。
#
# 删掉它们的理由不是"用不上了"，是**它们本身是一类缺陷的载体**：合成 state 的键集
# 是一张手抄表，而抄漏的形态是**消费端安静地少开一次火**——`round_state` 少一格
# `task_frames`（`_ledger_in_scope` 的析取少一条腿）、`popup_state` 少七格、其中
# `executed` 正是"令牌用在那一件上就作废"那一格（缺它 ⇒ 同一件写第二次又被放行）。
# 只要还有"合成一份 state"这个动作，就还有下一张手抄表。
#
# 两条声明留在上面，但它们的意思变了：
#   · `GATE_STATE_KEYS` / `POPUP_STATE_KEYS` —— 从"合成时该填哪些键"变成
#     **"这两个资产的读侧闭包必须全是 `AgentState` 的声明键"**（未声明的键被
#     langgraph 静默丢掉，本仓为此付过 13 次代价）。判据在 `test_frame_contract.py`。
#   · 逐条来源注释（`gate_node:13292` 之流）保留：它们是**取值的凭证**，
#     `test_frame_contract.py` 用 AST 从 `graph.py` 现取闭包与这里比对，对不上就红。
