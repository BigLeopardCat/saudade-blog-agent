# -*- coding: utf-8 -*-
"""帧契约：第二条臂（`agent/react_arm.py`）与生产（`graph.py` / `server.py`）之间的唯一事实源。

**为什么有这个模块**（20261016，`docs/adr/adr-0008-*`）。`agent/react_arm.py` 是**第二份
实现**：它用另一套循环（`create_agent` + 收敛判据 + 回执台账）产出**与生产逐字相同的帧**。
两套实现共享一份契约，而契约此前**只活在约定里**——三处症状，全都在这个仓里付过代价：

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

from langchain_core.messages import AIMessageChunk, ToolMessage

from agent.graph import plan_state

logger = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════════════════════
# 契约声明（下面这些常量是**声明**，不是缓存——`test_frame_contract.py` 从源码现取比对）
# ══════════════════════════════════════════════════════════════════════════════

# `server._run_agent_stream_to_queue` 按名字消费 `updates` 里**恰好这四个**节点。
# 臂产任何别的名字 = 那一格被生产者**静默丢掉**（它按名取键，取不到就不发对应的帧），
# 而报告里看不出是"臂没产"还是"生产者没认"。
NODE_FRAMES = ("planner", "execute", "model", "gate")

# `messages` 通道上**用户可见正文**挂的节点名。生产那条是 `model_node`（零工具 narrator）；
# 臂的正文不是从图里来的，但**必须冒充成同一个名字**——`run_golden.run_one` 只把
# `langgraph_node == "model"` 的分片拼进 `text`，改名等于每一条文本断言都被中间推理污染。
VISIBLE_NODE = "model"

# ── `gate_node` 的读侧闭包（含它调用的辅助函数）──────────────────────────────
# 这**不是**"它函数体里 grep 到的那七个"——那七个只是直接读的；下面另四个是辅助函数读的
# （`gate_node` 函数体里看不见，20261016 用 AST 走传递闭包实测）。逐条标出来源：
#
#   messages         gate_node:13292   —— 判"有没有执行事实"的唯一依据（它从消息流挑 ToolMessage）
#   plan             gate_node:13291   —— 分族判据的入参
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
# 静默不启用。臂今天**不产** `task_frames`（如实登记的缺口），所以这一格对它是恒空；
# 但**恒空与缺席不是一回事**：缺席是"没人喂"，恒空是"这一线确实没有任务"。写下来才分得清。
GATE_STATE_KEYS = frozenset({
    "blocked", "blocked_seen", "confirm_grant", "gate_replan", "ledger", "messages",
    "noop_specs", "pending_confirm", "plan", "receipts", "task_frames",
})

# ── `_confirm_popup` 的读侧闭包（含它调用的辅助函数）────────────────────────
# 弹卡那一格的形状判据（`_popup_state` 的 docstring 此前写的是"它只读 state 的四个键"——
# 20261016 实测**是十一格**，那四个只是 `_confirm_popup` **函数体里**直接读的）：
#
#   messages         _confirm_popup:12113 —— 喂 `_target_evidence`（"文章 id 有据"）
#   plan_obj         ← _plan_skill:12362 —— 令牌里签的技能名，空 ⇒ sign 拒签 ⇒ 卡弹不出
#   receipts         _confirm_popup:12260
#   ledger           _confirm_popup:12130（`_ledger_pending_text`）
#   executed         ← _grant_covers:8837 —— **20261012 第二次收窄那一格**
#   plan / blocked / blocked_seen / noop_specs / pending_confirm  ← _round_facts:3319-3347
#   confirm_grant    ← _all_granted:8853
#
# ⚠️ `executed` 是这一格里**最要紧**的一格，也是 20261016 之前**根本没喂**的一格。
# `_grant_covers` 拿它实现"令牌用在那一件上就作废"（20261012 第二次收窄，现场
# `20261012T195727`：同一件写静默落库三份）。缺了它 ⇒ `state.get("executed") or ()` 空 ⇒
# 那一格判据**恒不生效** ⇒ 臂这边"同一件第二次"又退回被令牌覆盖。
# **臂的 `executed` 必须现算**：生产由 `execute_node` 在请求内累积（`graph_input` 初值 `[]`），
# 而臂不往那份 state 里回写——本轮真执行过的调用只活在 `executor.calls` 里（`executed_specs`）。
POPUP_STATE_KEYS = frozenset({
    "blocked", "blocked_seen", "confirm_grant", "executed", "ledger", "messages",
    "noop_specs", "pending_confirm", "plan", "plan_obj", "receipts",
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
# 帧构造：四类 updates + 两路 messages
# ══════════════════════════════════════════════════════════════════════════════

def planner_frame(plan_obj: dict) -> dict:
    """`planner` 那一格（生产 `planner_node` 的 update 里 producer 只读 `plan_obj`）。"""
    return {"planner": {"plan_obj": plan_obj}}


def model_frame(m: Any) -> dict:
    """`model` 那一格（正文那条 `AIMessage` 的原文）。"""
    return {"model": {"messages": [m]}}


def gate_frame(**extra: Any) -> dict:
    """`gate` 那一格。`extra` 按调用方给的来：`fallback_text` / `gate_replan` / `done`。

    **不补默认值**——`{"done": False}` 与"不发 `done`"在 producer 那边是两件事
    （前者会覆盖上一轮的值，后者不动）。这类"补一个看起来无害的默认值"正是本仓
    `pydantic 空串缺省值` 那一族。
    """
    return {"gate": dict(extra)}


def execute_frame(*, receipts: Any, blocked: Any = (), **extra: Any) -> dict:
    """`execute` 那一格（回执/受阻行/弹卡载荷都走这里）。

    `receipts` 是**累计语义**（生产 `execute_node` 每次交的都是请求内全部 PASS 行，
    producer 靠 `receipt_sent` 下标算增量）⇒ 调用方必须给全量，不是增量。
    `blocked` 同理。`extra` 不补默认值，理由同 `gate_frame`。
    """
    return {"execute": {"receipts": list(receipts or []),
                        "blocked": list(blocked or []), **extra}}


def visible_chunk(text: str) -> tuple[str, Any]:
    """`messages` 通道上**用户可见**的那一条分片（正文）。

    与"发不发"无关——空正文由调用方决定不发（`_on_model` 那条注：补一段话掩盖
    "模型没说"会让 `nonempty` 断言假绿）。这里只管形状，含 `langgraph_node` 那一格
    （见 `VISIBLE_NODE` 的注：它决定这段字进不进 `text`）。
    """
    return ("messages", (AIMessageChunk(content=text), {"langgraph_node": VISIBLE_NODE}))


def tool_frame(i: int, c: dict) -> ToolMessage:
    """一次已发生调用 → 工具帧（`messages` 通道那一条 `ToolMessage`）。

    单独成函数只为**一处措辞**：文章读取快道要把它喂给模型（系统提示的 `tool_results`），
    `_on_tools` 要把它发给用户/报告——两处各自拼一份字符串，改一处漏一处就是一条新漂移源。

    `tool_call_id` 用 `react_{下标}`：内层 `create_agent` 自己发的 `ToolMessage` 有它
    自己的 id，这里不需要与任何 `AIMessage` 的 `tool_calls` 对上（golden 只读 `name` 与
    `content`），造一个稳定的即可。
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
# state 塑形：把"最小 state"喂给生产那两个资产（`gate_node` / `_confirm_popup`）
# ══════════════════════════════════════════════════════════════════════════════

def plan_for_gate(plan_obj: dict) -> dict:
    """`plan_obj` → **能被 `plan_state` 编码**的那份（缺键补上，不静默）。

    `plan_state`（= `plan_encode` 的唯一调用点，`tests/test_plan_channel.py` 用源码锁
    钉着）要 `skill` 与 `reply` 两个键，缺一个就是 `KeyError`。本线的 `_plan_of` 在
    **一条工具调用都没发生**时返回 `{}`（纯闲聊轮），那时生产那边的技能名是 `chat`
    ——按它补，别填空串：`gate_node` 拿 `plan["skill"]` 当分族判据的入参，空串会退化
    成宽查（漏判，方向安全但读数与生产不是同一档）。
    """
    obj = dict(plan_obj or {})
    obj.setdefault("skill", "chat")
    obj.setdefault("tools", [])
    obj.setdefault("note", "")
    obj.setdefault("reply", "")
    return obj


def popup_state(state: dict, plan_obj: dict, ledger: Any, messages: list,
                executor: Any) -> dict:
    """喂给 `graph._confirm_popup` 的最小 state（`POPUP_STATE_KEYS` 那十一格）。

    `messages` 必须带上**本轮已执行工具的真结果**：`_target_evidence`（"文章 id 有据"
    那条判据）从 `ToolMessage` 里找材料，喂空的会让文章族写操作的目标判据恒假 ⇒ 卡弹不
    出来——方向是**假阴性**，恰好是"评测里做不到生产做得到的事"，而报告上看起来像
    "这臂不会弹卡"。`executor.calls` 就是这一轮的逐次真调用（工具名 + 结果原文）。

    **其余七格从 `state` 原样透传**（20261016 补）：生产那一轮它们都是真值（`graph_input`
    给了初值、`execute_node` 在请求内累积），臂此前只挑了自己造的四格、把剩下的丢了
    ——`_round_facts` / `_grant_covers` / `_ledger_pending_text` 于是各自读到空。
    `executed` **例外，必须现算**（见 `POPUP_STATE_KEYS` 的注）：`state` 里那份是请求初值，
    臂的真执行只活在 `executor.calls` 里，两者相加才是"本轮已执行过什么"。
    """
    calls = list(getattr(executor, "calls", []) or [])
    extra = [ToolMessage(content=str(c.get("result") or ""),
                         name=str(c.get("tool") or ""),
                         tool_call_id=f"popup_{i}") for i, c in enumerate(calls)]
    src = state or {}
    return {
        "messages": list(messages or []) + extra,
        # `_plan_skill` 只读这一格，而令牌里签的技能名就是它：空 ⇒ `sign` 拒绝签发
        # ⇒ 卡弹不出来（所以 `stream` 里按"最近一次有效计划"记着）。
        "plan_obj": dict(plan_obj or {}),
        "receipts": list(ledger.receipts),
        "confirm_grant": src.get("confirm_grant"),
        "executed": list(src.get("executed") or []) + executed_specs(calls),
        "ledger": src.get("ledger") or {},
        "plan": src.get("plan") or "",
        "blocked": list(src.get("blocked") or []),
        "blocked_seen": list(src.get("blocked_seen") or []),
        "noop_specs": list(src.get("noop_specs") or []),
        "pending_confirm": src.get("pending_confirm"),
    }


def round_state(state: dict, plan_obj: dict, ledger: Any, messages: list,
                executor: Any, reply: Any = None, *, replan: bool = False) -> dict:
    """本轮的最小 state——**两个消费端读的是同一批键**（20261011 起 gate，20261012 起 narrator）。

    `reply=None`（narrator 那一支）**不放答复**：生产 narrator 看不到任何"草稿"（它读的是
    计划契约与工具帧），把决策模型那条消息塞进去就是把 P6 拆掉的那件事又接回去。
    闸门那一支必须带（`_repeat_reason` 的复读比对要看得见它）。

    做法与 `popup_state` 同族，但**这两处合成了一份**（20261012）：`_narrator_plan` 与
    `gate_node` 读的是同一批键——账甲 `_round_facts` 那一族正是两个消费端共用的合成函数，
    "两边各拼一份 state"必然在某一天让它们对**同一轮**算出不一样的形状。

    **键集由 `GATE_STATE_KEYS` 声明、由 `tests/test_frame_contract.py` 从 `graph.py`
    现取比对**（20261016 起）——此前那张表是用 `awk`+grep 从 `gate_node` 函数体里数出来
    的手抄表，少了一格（`task_frames`），而少一格的形态是**闸门安静地少开火**。

    三处取法要**说准**，它们各自都有一个假读数：

    · `messages` **必须把本轮的工具帧放进去**——`frames` 是 `gate_node` 判"有没有执行
      事实"的唯一依据（它从消息流里挑 `ToolMessage`），喂空的会让**每一族声称判据**都
      判成"零帧轮"（方向是**过严**：一条真办成了事的回复会被当成零帧轮按更宽的族去查）。
      过闸门那一支还要把**答复本身**放进去（`reply=`），`_repeat_reason` 的复读比对才
      看得见它；narrator 那一支不带（见上）。
      帧用 `tool_frame` ——**与发到用户/report 的是同一份**：闸门判的对象必须是主人
      真看到的那些帧，另造一份措辞不同的一次性帧＝判据与事实分家。
    · `plan` 走 **`plan_state(...)`**（不是 `plan_encode` 直调——那个函数有源码锁，只许
      `plan_state` 调；也不是自己拼 `SKILL=` 行——那是第二份排版）。
    · `ledger` 原样透传（洞⑦ 的台账否认判据读它，`graph_input` 装的就是这一格）。
    · `gate_replan` 是**上一圈有没有重规划过**，由调用方给。
    """
    src = state or {}
    frames = [tool_frame(i, c) for i, c in enumerate(getattr(executor, "calls", []) or [])]
    blocked = [blocked_row(b) for b in ledger.blocked]
    return {
        "messages": (list(messages or []) + frames
                     + ([reply] if reply is not None else [])),
        **plan_state(plan_for_gate(plan_obj)),
        "receipts": list(ledger.receipts),
        # 账甲（`_round_facts`）与 `_blocked_article_targets` 读的三格：见 `GATE_STATE_KEYS`。
        # `blocked_seen` 的键形与 `execute_node` 的 `_blocked_key` 逐字同款（`工具::原因码`）
        # ——生产那一支是**跨轮累积**，本线的 `ledger.blocked` 同样只增不减，所以这一次
        # 运行的范围就是同一种累积。
        "blocked": blocked,
        "blocked_seen": sorted({f"{b.get('tool')}::{b.get('reason')}"
                                for b in ledger.blocked}),
        "confirm_grant": src.get("confirm_grant"),
        "ledger": src.get("ledger") or {},
        "pending_confirm": src.get("pending_confirm"),
        "noop_specs": src.get("noop_specs") or [],
        # `task_frames` 原样透传（20261016 补）：`_ledger_in_scope` 的析取里那一条腿。
        # 臂今天不产任务帧 ⇒ 它恒空；但**恒空**（这一线确实没有任务）与**缺席**
        # （没人喂）在读侧不是一回事，后者让闸门安静地少开一次火。
        "task_frames": list(src.get("task_frames") or []),
        "gate_replan": bool(replan),
    }


def gate_state(state: dict, plan_obj: dict, ledger: Any, messages: list,
               executor: Any, reply: Any, *, replan: bool) -> dict:
    """喂给 `graph.gate_node` 的最小 state——键集与理由都在 `round_state`（P6 起它只是壳）。"""
    return round_state(state, plan_obj, ledger, messages, executor, reply=reply,
                       replan=replan)
