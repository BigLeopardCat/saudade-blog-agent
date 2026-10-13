# -*- coding: utf-8 -*-
"""golden 的第二条臂：把 ReAct 试验线接进评测框架（`GOLDEN_ARM=react`）。

**它是什么**：`eval/golden_arm.py::build_agent()` 在 `GOLDEN_ARM=react` 时 import 本模块的
`build()`，把返回的对象装在 `server._agent` 上，生产那半
（`server._run_agent_stream_to_queue`）当图来消费。**生产代码一个字节都不改**——所以本模块
的全部工作可以一句话说完：

    用另一套循环（`agent.react_line` 的 `create_agent` + 收敛判据 + 回执台账），
    产出**一模一样的那几类帧**。

**缝为什么必须长这样**：生产者按名字消费 `updates` 里恰好四个节点
（`model` / `planner` / `execute` / `gate`），并只把 `langgraph_node == "model"` 的
`messages` 分片当用户可见正文。`create_agent` 的内层节点也叫 `model`/`tools`、输入 schema
只有 `{"messages": …}`，直接换对象会让 `commands`/`exec_tools`/`confirm_payloads`/`resets`
全空、正文被中间推理污染（`docs/` 里那份 2.1 的推导）。所以这里是**外层薄适配器**：
内层照跑，外层重新产帧。

三条**铁律**（每一条都有对应的假读数，见 `eval/arm_capability.py` 要防的那一族）：

  ① **只有最终答复上 `messages` 通道**。`run_golden.run_one` 把每个 `AIMessageChunk`
     拼进 `text`——ReAct 的中间轮里模型会写"让我先看看…"，转发它 = 污染每一条文本断言。
  ② **`ToolMessage` 必须带真工具名**。内层工具是**技能级**的（`chat`/`content_query`…），
     转发它们会让 `tool_calls` 记成技能名，而 `require_tool_calls`/`require_zero_exec`
     认的是 `_TOOL_REGISTRY` 里的真名 ⇒ 那两族会**静默失真**（`require_zero_exec` 更坏：
     写工具永远不在 `tool_calls` 里 ⇒ 恒绿）。所以 ToolMessage 由 `executor.calls`
     （逐次真调用）重建。
  ③ **回执按生产形状**（`tool/args/result/ts` + PASS 行的 `cmd`）——`__EXEC__`/`__CMD__`
     两条帧、以及那 131 条 `*/cmd_*` 断言的全部输入。

**写操作确认卡（P3，20261005）**：写 spec 卡在同意闸上时——`SkillExecutor` 判
「需要同意 ∧ 这一轮没获同意」⇒ **不执行**、记进 `ledger.pending`（判据与
`graph.execute_node` 的 `consent_missing` 逐字同款，见 `_consent_missing`），臂随即
调 **`graph._confirm_popup` 本身**（不抄第二份排序/快照/滤空逻辑）拿到
`pending_confirm` / `pending_action` / `confirm_text`（或"状态已达成"那支的
`noop_text`/`noop_note`），原样交回 producer ⇒ `__CONFIRM__`/`__PENDING__` 两条控制帧
由生产那半发。弹卡轮**到此为止**（与 `route_after_execute` → END 同义），正文由
producer 用 `confirm_text` 发**一次**——臂这边一个字节的正文都不补，补了就是两遍。

**narrator 资产（P6 前半，20261005）**：系统提示从生产的 narrator 资产里接了
`STICKER_GUIDE`（12 个贴纸名的唯一名字表，**逐字共用**）——不接它 `:害羞:` 这类记号
结构上产不出来，`sticker_*` 一族恒红。当时还接了一份 `NARRATOR_DISCIPLINE`（纪律 1–23）
并配一段立场改写；**20261013 批次 B 两份一起摘掉了**：那份纪律是给**零工具节点**写的
（第 1 条"你没有任何可以直接调用的工具"），而 P6 起收尾正文归独立 narrator，它读的
`_EXECUTOR_PROMPT` 里本来就带这份纪律（**一个字没动**）⇒ 决策模型再带一份只剩自相矛盾。
`own_*` 那族的"如实说读不到、却又多派一句『你先去登录』"改由 narrator 那半的纪律 20 管
（也是它原本的岗位），另在循环机制第 6 条留了一句「站里的事你有工具能查，别把活派回
给主人」。

**闸门谓词（P4，20261011 已接）**：这条臂此前**结构上不会发 fallback** ⇒
`arm_capability --arm react` 判 `comparable = False`（372 个可证伪键实例里 138 条落在
`forbid_fallback`，全白发）⇒ 那唯一一次全量读数（`20261005_203521_332`，0.9216）
**不可读、不许引用**。接法见 `ReactGoldenArm._gate`：**直调 `graph.gate_node`**，
arm 侧只做两件事——按它读的键塑形一份最小 state（`_round_state`：函数体里七个，
**外加辅助函数读的三个**，见那里的注），把返回翻成帧。判据（`_REPLAN_ISSUES` 约 20 族 /
`_claim_issue` / `_fallback_result` / `_replan_result`）**一条都没抄**：抄第二份就是抄
一条漂移源，而它恰好是本仓最常见的缺陷。

**narrator 独立节点（P6，20261012 已接）**：收尾不再由决策模型自己写——内层循环给出
"没有工具调用了"那条消息（`pending_final`）之后，正文改由**独立的零工具调用**产出
（`_narrate` / `_narrator_call`），提示词是生产 narrator 那一份
（`graph._EXECUTOR_PROMPT`，含 `NARRATOR_DISCIPLINE` 与 `STICKER_GUIDE` 两块共享资产），
**决策模型那条草稿不进提示词**（生产 narrator 也看不到任何草稿），只在 narrator 两次都
拿不出正文时兜底。**闸门判的是 narrator 写出的那一段**——这正是批 1 的意义所在：同一条
消息流里"决策模型自己写收尾"时，它手上就有工具，"我调用了 X"既可能是回忆也可能是谎言；
生产那个被判的模型是**零工具节点**，那句话从它嘴里出来必然是声称。判的对象不同种，
两臂就不可比（批 1 只把 fallback 那条路接上了，这一条才是把"判谁"接上）。
**P6 是行为改动** ⇒ 本线自此与 `20261005_203521_332` 那份读数**不可比**，旧读数在
文档里一律标注为「P6 之前」。

**这一版仍然不做的**（别当成已经做完了）：
  · `task_frames`（task_state 族）⇒ **不发**。能力台账里这一族只有 1 条断言
    （`task_state_resume_settle`），而它的兄弟判据 `require_task_state` 由生产那半的
    确定性结算满足（`server.py` 的 `rows_to_settle`）⇒ 这一族在本线上是 `armed` 而
    **没有**变成 `comparable = False` 的来源（20261012 实测）。如实登记为未做。
  · **trace 的节点事件这一族**（20261012 全量双臂实测抓出）：本线每轮**只**留下
    `producer.*` 与 `gate.*` 两类事件（gate 那几格是直调 `graph.gate_node` 顺带记的），
    生产那半每轮的 `planner.llm_done` / `native_decision` / `decision` /
    `execute.call` / `execute.check` **一个都没有** ⇒ L3 跨源对账
    （`eval/trace_reconcile.py`）对本线近乎无料可读。
    **判据后果只兑现了一处、且已修**：`planner.ledger_frame` 缺席让
    `require_ledger_frame` / `require_ledger_rows` / `require_card_targets_from_ledger`
    三条断言在本线上**恒假**（全量里 `admin_board_audit_reviewed_refusal` 因此报红，
    而两臂那一轮的行为**逐字相同**——回复正文一字不差、卡载荷的 skill/tool/args 全同）
    ⇒ `_system_prompt` 现在照 `graph.py` 的 planner 那一处记同款事件，判据
    `tests/test_react_arm.py` ⑬（含负控）。这与 `forbid_fallback` 是同一个病的两个方向：
    那边"做不到 ⇒ 纯否定断言恒真"（假绿），这边"不产证据 ⇒ 要求型断言恒假"（假红）。
    **其余节点事件的补记仍未做**——它今天一个读数都不改变（没有断言读它们），补它是为
    将来 L3 与本臂真上线，不是为这份读数。
  · （20261012 划掉两条此前记在这里的"缺口"——**实测都不是缺口，所以一个字都没改**：
    · `__EXEC__` **已经在发**：`_on_tools` 按生产形状产的 `receipts` 里就带着 PASS 行的
      `cmd`，而 `__EXEC__` 是 `server._run_agent_stream_to_queue` 从 updates 里读的
      ——挂真生产者跑过一次，帧在；
    · 跨轮执行记忆（`recent_executions`）与 `open_tasks` **已经读到了**：
      `server._build_messages` 把它们写进那条 `[System: …]` 块，`_page_ctx` 把整块提
      出来，`_system_prompt` 的**事实格**把它交给模型（与生产 planner/model
      两条腿同一个通道）。**不另写第二个渲染器**——同一份事实两个渲染器就是漂移。）
  · **动作事实块这一格 20261005 起不再是缺口**：生产两臂都停止代印（
    `agent/factblock.py` 的 `BLOCK_FAMILIES` 空集）⇒ "叙述权收归系统"这件事整体歇业，
    本线"没接它"反而与 graph 臂口径一致了（此前它是两臂的一个真实差异）。

**已知口径偏差**（读第一批读数时必须带着看）：
  · `parallel_tool_calls` 没有钉住——`create_agent` 自己 bind_tools，生产 planner 是
    `parallel_tool_calls=False`（见 `native_plan.bind_native` 的注）。本适配器**容忍**
    一轮多条调用（合并成一份 `plan_obj`、逐条发 `execute`），所以偏差表现为"步子更碎"，
    不会丢调用；
  · 内层只看得到 `[HumanMessage(本轮用户消息)]`（与被测的 graph planner 信息面同源：
    page_ctx / recent_tail / doc_anchors 都在系统提示里）。文本按 `_last_user_msg` 取末
    500 字，**非文本部件（图片）原样带着**（见 `_inner_message`）。**不能**把整条生产
    消息流塞进去——`ConvergenceMiddleware` 数的是 state 里 `AIMessage` 的条数，历史轮次的
    AI 消息会把轮数预算吃光（多轮用例会第一轮就"预算用满"收尾，读数看着像"这臂什么都办不成"）。
"""
from __future__ import annotations

import json
import logging
from typing import Any, Iterator

from langchain_core.messages import (AIMessage, AIMessageChunk, HumanMessage,
                                     SystemMessage, ToolMessage)
from langchain_core.tools import StructuredTool

from agent.context import (_doc_anchors, _frame_texts, _last_assistant_utterance,
                           _last_user_msg, _page_ctx, _recent_tail, _receipts_text,
                           _short_reply_hint, with_tool_call_pairs)
from agent.decisions import _article_fast_path
from agent.factblock import block_of, is_action_family, is_block_family
from agent.graph import (_EXECUTOR_PROMPT, _NARRATOR_NO_TOOLCALL_NUDGE,
                         _NO_PRINTED_FACTS, _NO_PRINTED_FACTS_ACTION, _confirm_popup,
                         _narrator_plan, _pending_ledger_frame, _principal_of,
                         gate_node, plan_state)
from agent.native_plan import build_tool_schema
from agent.prompts import BLOG_ASSISTANT_PROMPT, STICKER_GUIDE, audience_block
from agent.react_line import RunLedger, SkillExecutor, build_agent
from agent.skills import instantiate_plan
from agent.stickers import repair_sticker_tokens
from config.settings import settings
from models.llm import get_llm
from tools import get_all_tools
from utils.trace import record

logger = logging.getLogger(__name__)

ARM_NAME = "react"

# 收敛中间件在 `stream(..., stream_mode="updates")` 里的**节点名前缀**。它注入的收尾
# 正文挂在这两个名字下面（`.before_model` / `.after_model`），**不叫 "model"**——
# 20261005 实测，别凭"应该是 model"去猜。
_CONVERGENCE_NODE_PREFIX = "ConvergenceMiddleware"


def _is_convergence_node(node: object) -> bool:
    """这个 update 是不是收敛中间件发的那一格（见 `stream` 的接线注）。"""
    return str(node).startswith(_CONVERGENCE_NODE_PREFIX)


def _tool_frame(i: int, c: dict) -> ToolMessage:
    """一次已发生调用 → 工具帧（`messages` 通道那一条 ToolMessage）。

    单独成函数只为**一处措辞**：文章读取快道（`ReactGoldenArm.stream`）要把它喂给
    模型（系统提示的 `tool_results`），`_on_tools` 要把它发给用户/报告——两处各自
    拼一份字符串，改一处漏一处就是一条新漂移源。

    `tool_call_id` 用 `react_{下标}`：内层 `create_agent` 自己发的 ToolMessage 有它
    自己的 id，这里不需要与任何 AIMessage 的 tool_calls 对上（golden 只读 `name` 与
    `content`），造一个稳定的即可。
    """
    return ToolMessage(content=str(c.get("result") or ""),
                       name=str(c.get("tool") or ""),
                       tool_call_id=f"react_{i}")


def _inner_message(messages: list, user_msg: str) -> HumanMessage:
    """内层循环看到的那**一条**用户消息：文本仍取 `user_msg`（末 500 字的口径），
    **但把多模态部件（图片）原样带过去**。

    病（20261005 实测，`image_color_red`/`image_two_colors` 两条慢性红）：主人发的消息
    `content` 是 `[{"type":"text",…},{"type":"image_url",…}]`，而本适配器此前写的是
    `HumanMessage(content=user_msg)`——**按文本重建等于把图整段丢掉**，模型看不见图，
    自然答不出"这张图是什么颜色"，还会顺手去调 `get_blog_info`（那条用例 `no_tool_calls`
    也跟着红）。生产 narrator 读的是**原消息**，所以这是本层自己要修的口径差。

    只补回**非文本部件**、文本仍用 `user_msg`：`[System: …]` 那半已被 `page_ctx`
    提取进系统提示，再塞一遍是重复注入——那会让**每一条**用例的提示词都变，读数就从
    "修了图片这一件事"变成"什么都变了"（一次只动一个变量）。
    """
    last = next((m for m in reversed(messages or [])
                 if isinstance(m, HumanMessage)), None)
    content = getattr(last, "content", None)
    if not isinstance(content, list):
        return HumanMessage(content=user_msg)
    extra = [p for p in content
             if isinstance(p, dict) and p.get("type") not in ("text", "input_text")]
    if not extra:
        return HumanMessage(content=user_msg)
    return HumanMessage(content=[{"type": "text", "text": user_msg}] + extra)


# 模型决策轮上限。生产 `MAX_PLAN_ROUNDS=4`；这里放宽到 6 是本线既有的取值
# （`react_line.ConvergenceMiddleware` 的注：一轮里可能并发多条调用，步子比 planner 碎），
# 离线 A/B 用的也是 6 ⇒ 与既有读数可比。
BUDGET = 6
# 图级保险丝（同离线 A/B 的 REC_LIMIT）：预算判据自己会收尾，这一条只是兜底。
REC_LIMIT = 24

class ReactGoldenArm:
    """`server._agent` 上的那一层薄外层（见模块头注的三条铁律）。"""

    name = ARM_NAME

    def stream(self, state: dict, config: dict | None = None,
               stream_mode: object = None) -> Iterator[tuple[str, Any]]:
        """接 `graph.graph_input(...)` 的 25 键 state，产 `(mode, data)` 二元组。

        `stream_mode` 收下但**不用**：生产者要的就是 `["messages", "updates"]` 两路，
        本适配器恒产这两路（收下参数只是为了让签名与图一致，签名对不上会以
        `TypeError` 的形态炸在生产者里）。
        """
        messages = list((state or {}).get("messages") or [])
        principal = _principal_of(config)
        role = principal.known_role
        user_msg = _last_user_msg(messages)

        ledger = RunLedger()
        # 同意闸的两个输入（P3）：主人这一轮的原话 + "刚在卡上点过确定"的凭据。两者
        # 都从**生产给的那一份**里取（`state["confirm_grant"]` 由 `graph_input` 装、
        # `user_msg` 与 planner 读的同一句）——不另设来源，否则弹卡判据会与执行判据分家。
        grant = (state or {}).get("confirm_grant")
        executor = SkillExecutor(_real_tools(config), ledger, role=role,
                                 principal=principal, user_msg=user_msg, grant=grant)

        # ── 当前文章读取快道（20261005，照搬 planner 的 `_article_fast_path`）──────
        # 生产在 planner 的首轮、**任何 LLM 之前**判它（`graph.py` 里
        # `rounds == 0 and not has_frames` 那道判据）：主人当前页是文章详情页且这句引用了
        # "这篇/我正在读"，系统就**强制**读那篇（article_id 从 current_url 解析，
        # 不过模型的手）——那是"零工具却声称读过了"在结构上不可能的那一格。
        #
        # 本线此前没有它，代价实测过（20261005）：`todo_multi_step_serial`（「我正在读
        # 这篇架构文章，顺便带我去留言板看看」）react **6/6 红**、graph 6/47（13%）。
        # 红的形态是**只做后半件**（导航去留言板）、前半件被静默丢掉。把"先读再走"
        # 留给模型自己记，就是让它猜；生产早就不猜了。
        #
        # **顺序与生产同源**：读发生在内层循环**开始之前**。这也正是它的守卫——
        # 生产那条是 `rounds == 0 and not has_frames`，本线没有"轮"这个坐标，
        # 等价物就是"进循环之前只判一次"（进循环后再判＝生产注里写的那个死循环陷阱）。
        fast_plan = _article_fast_path(user_msg, _page_ctx(messages, role))
        if fast_plan:
            logger.info("[react_arm] 文章读取快道命中，强制读 article_id=%s（零 LLM）",
                        (fast_plan.get("params") or {}).get("article_id"))
            executor.run("read_article", dict(fast_plan.get("params") or {}))
        # 这一次读的工具帧要同时喂两处：模型（系统提示的 `tool_results`）与用户/报告
        # （`messages` 通道的 ToolMessage）。**只构造一次**：同一份 ToolMessage 对象
        # 喂两边，措辞才不会两处各说各的（`_on_tools` 也用同一个 `_tool_frame`）。
        fast_frames = [_tool_frame(i, c) for i, c in enumerate(executor.calls)]

        agent, ledger = build_agent(
            _llm(), _skill_menu(role),
            system_prompt=_system_prompt(messages, role, principal, config, user_msg,
                                         fast_frames=fast_frames),
            budget=BUDGET, executor=executor, ledger=ledger, name="react_arm")

        # 快道的帧在这里补发（内层循环里那次 `tools` 节点只发**增量**）：位置与生产
        # 同源——execute 的帧先到，narrator 的正文后到。
        sent_calls = len(executor.calls)
        if fast_frames:
            yield from self._on_tools(executor, ledger, 0)
        last_plan: dict = {}
        # 收尾候选（20261005）：模型给出一条**不带工具调用**的答复 ≠ 这一轮就此结束
        # ——`ConvergenceMiddleware.after_model` 可能把它**打回**（零工具收尾但主人的
        # 那句话里还有没做过的动作 ⇒ 纠偏重决策一次）。既然可能被打回，就**不许当场
        # 发帧**：`run_one` 的 `text` 是 messages 通道上每一条分片的拼接，先发的那条
        # 草稿会与真正的最终答复**连在一起**交给判据与主人（"消息壳架空判据"那一族）。
        # 所以零工具答复先扣在这里，**循环结束才发**；被打回时由后一条覆盖。
        #
        # **闸门打回也是一条"重来一次"**（P4，20261011）：`gate_node` 判不过 `_REPLAN_ISSUES`
        # 那一族时返回 `gate_replan`，生产那边由 `route_after_gate` 走回 planner。本线没有
        # 那条边，等价物是**再 invoke 一次内层**（只此一次，见 `_gate` 的注），所以外层是个
        # 两圈封顶的循环；`notes` 就是 `_replan_result` 那条确定性提示。
        notes: list = []
        replan_used = False
        while True:
            pending_final: AIMessage | None = None
            try:
                # 内层只看得到本轮那一句（理由见模块头注的最后一条口径偏差）——但那一句
                # **带着它的多模态部件**（见 `_inner_message`：按文本重建会把图片丢掉）。
                # 重规划那一圈把 gate 的提示接在同一条 HumanMessage 之后（`_replan_result`
                # 已经把被否定的那条 AI 消息从生产 state 里摘掉了；这里内层每次 invoke 都
                # 从空 state 起，被否定的那条**天然不在**上下文里，与生产同形）。
                for step in agent.stream(
                        {"messages": [_inner_message(messages, user_msg)] + notes},
                        {"recursion_limit": REC_LIMIT},
                        stream_mode="updates"):
                    for node, upd in (step or {}).items():
                        msgs = list((upd or {}).get("messages") or [])
                        if node == "tools":
                            sent_calls = yield from self._on_tools(
                                executor, ledger, sent_calls)
                            # 写操作卡在同意闸上 ⇒ 这一轮到此为止（P3）。位置与生产同源：
                            # `graph.execute_node` 是"逐 spec 循环**之前**判一次"，本线
                            # 是"这一批工具轮跑完、模型再决策之前"判一次——两者都保证
                            # **一张卡不带任何半截执行**。
                            popup = self._pending_popup(ledger, executor, state, last_plan,
                                                        messages, principal, user_msg, config)
                            if popup is not None:
                                yield from self._on_popup(popup, ledger)
                                return   # 与 graph 的 `route_after_execute` → END 同义
                            continue
                        for m in msgs:
                            if not isinstance(m, AIMessage):
                                continue
                            if node == "model":
                                if not m.tool_calls:
                                    # 零工具 = 收尾候选：**扣着不发**（见 `pending_final` 的注）。
                                    pending_final = m
                                    continue
                                # 这一轮点名的技能（`plan_obj`）：`_plan_skill` 只读它，
                                # 而令牌里签的技能名就是它——取不到 ⇒ 空串 ⇒ `sign` 拒绝
                                # 签发 ⇒ 卡弹不出来。所以按"最近一次有效计划"记着。
                                plan = _plan_of(m.tool_calls, role)
                                if plan:
                                    last_plan = plan
                                yield from self._on_model(m, role, plan)
                            elif _is_convergence_node(node):
                                # 收敛中间件注入的**确定性收尾**（`ConvergenceMiddleware._stop`
                                # 返回 `{"jump_to": "end", "messages": [AIMessage(收尾正文)]}`）。
                                # 它挂在中间件自己的节点名下（`ConvergenceMiddleware.after_model`
                                # / `.before_model`），**不叫 "model"** —— 20261005 实测（脚本化假
                                # 模型跑 `build_agent` 的原始 update 流）：只认 "model" 会把这条
                                # 正文**静默丢掉**，用户拿到的是**空回复**，而报告里看不出是
                                # "模型没说"还是"适配器没转发"。这不是边角：撞预算与"同一个调用
                                # 重试两次"是本线**唯一**的两条收尾路径（`ConvergenceMiddleware`
                                # 的全部判据都在这里），丢掉它 = 那两类运行全部记成"什么都没说"。
                                # 确定性收尾取代任何扣着的草稿：那一条才是这一轮的事实。
                                pending_final = None
                                yield from self._on_wrap_up(m)
            except Exception as e:  # noqa: BLE001 —— 内层炸了也要给主人一句诚实的话
                # 不把异常放走：golden 一条用例炸掉会让整轮跑丢一条读数，而"这臂办不成事"
                # 与"这臂崩了"在报告里必须**长得不一样**。所以照样产一条收尾正文（确定性、
                # 不含任何执行声称），并留 WARNING（原样吞掉才是真的查不出来）。
                logger.warning("[react_arm] 内层循环异常，按诚实收尾处理：%s", e,
                               exc_info=True)
                yield from self._crash_tail(ledger, e)
                return
            # 循环正常走完 ⇒ 扣着的那条零工具消息就是**这一轮的收尾信号**（只有它没被打回
            # 才会走到这里；打回过的那条已被后一条覆盖）。位置在循环**之后**是有意的：
            # `run_one` 的 `text` 按帧序拼接，草稿若先发就会与最终答复连成一串。
            # 它**不是**发给主人的那段正文（P6 起由 narrator 写），但它**先过闸门**（P4）
            # ——这就是这条臂此前那 138 条 `forbid_fallback` 恒真的洞。
            if pending_final is None:
                return
            # P6：最终答复**不由决策模型自己写**——它那条零工具消息只是"我这边够了"
            # 的信号，正文交给独立的零工具 narrator（`_narrate`，与生产 `model_node`
            # 同一份提示词资产）。**这一步在闸门之前**：闸门判的必须是主人真会读到的
            # 那一段，拿决策模型的草稿去过闸 = 判的对象与生产不是同一种东西（那正是
            # 批 1 接了闸门却仍不可比的原因）。
            reply = self._narrate(pending_final, last_plan, ledger, state, messages,
                                  executor, config, role)
            verdict = self._gate(reply, last_plan, ledger, state, messages,
                                 executor, config, replan_used)
            if verdict["kind"] == "replan":
                replan_used = True
                notes = list(verdict["messages"])
                yield ("updates", {"gate": {"gate_replan": True, "done": False}})
                continue
            if verdict["kind"] == "fallback":
                # 两条通道都要发：**流式**那半读 updates 的 `fallback_text`（发 `__RESET__`
                # 并把正文换成兜底文本），**同步**那半只认 messages 通道上那条
                # `[Fallback 决定]` SystemMessage（`_run_agent_sync`）。少发一条 = 那一半
                # 的 fallback 静默失效，而另一条通道看起来完全正常。
                yield ("messages", (SystemMessage(
                    content=f"[Fallback 决定]: {verdict['text']}"), {}))
                yield ("updates", {"gate": {"fallback_text": verdict["text"],
                                            "done": True}})
                return
            yield from self._on_model(reply, role, {})
            return

    # ── narrator（P6，20261012）────────────────────────────────────────
    def _narrate(self, draft: AIMessage, plan_obj: dict, ledger: RunLedger, state: dict,
                 messages: list, executor: SkillExecutor, config: dict | None,
                 role: str | None) -> AIMessage:
        """最终答复由**独立的零工具 narrator** 写（模块头注里那条 P6 缺口的闭合点）。

        **为什么必须独立**（这才是批 1 的意义）：闸门判的是**叙述的声称**与执行事实对不对
        得上，而生产那个被判的对象有一个结构性质——它是**零工具节点**（`model_node` 不
        `bind_tools`），"我调用了 X"这句话从它嘴里出来必然是声称，不可能是事实。本线此前
        是**同一条消息流里决策模型自己写收尾**：它手上就有工具，同一句话既可能是回忆也可能
        是谎言，判据判的东西就与生产不是同一种了。拆开之后三件事各归各位：决策模型只决定
        "够了没有"，narrator 只负责说，闸门只判后者——两臂才第一次可比。

        **草稿（`draft`）不进提示词**（与生产同形）：生产 narrator 看不到任何"草稿"，它读的
        是计划契约与工具帧。本线把决策模型那条零工具消息当作"这一轮到此为止"的信号，
        正文**重写**——这就是"叙述权收归 narrator"。草稿只在 narrator 两次都拿不出可用正文
        时兜底（`_narrator_call` 返回 None），理由见那里的注。

        异常**吞掉退回草稿**：narrator 是加装的一层，它自己炸了不该把主人这一轮的答复整条
        吃掉（与 `_gate` 的兜底同一条理由，方向也一样——宁可拿一条没经过 narrator 资产的
        话，也不给主人一句"卡住了"）。
        """
        rst = _round_state(state, plan_obj, ledger, messages, executor)
        receipts = [r for r in rst["receipts"] if isinstance(r, dict)]
        drop = {str(r.get("tool") or "") for r in receipts if is_block_family(r)}
        try:
            out = _narrator_call([SystemMessage(
                content=_narrator_prompt(rst, receipts, drop, config, role))]
                + with_tool_call_pairs(list(rst["messages"])))
        except Exception as e:  # noqa: BLE001 —— 见 docstring 最后一段
            logger.warning("[react_arm] narrator 调用异常，退回决策模型那条草稿：%s", e,
                           exc_info=True)
            return draft
        if out is None:
            logger.warning("[react_arm] narrator 两次都没给出正文，退回决策模型那条草稿")
            return draft
        return out

    # ── 帧合成 ───────────────────────────────────────────────────────
    def _on_model(self, m: AIMessage, role: str | None,
                  plan: dict | None = None) -> Iterator[tuple[str, Any]]:
        """内层的一次模型轮：要么是**决策**（带 tool_calls），要么是**最终答复**。

        `plan` 由调用方算好传进来（`_plan_of`，同一份 `plan_obj` 还要给弹卡那一路用
        ——`_plan_skill` 只读它）。这里再算一遍就是两份实现，改一处漏一处。
        """
        if m.tool_calls:
            if plan:
                yield ("updates", {"planner": {"plan_obj": plan}})
            return
        text = str(m.content or "")
        if not text:
            # 空收尾（模型给了个空消息）：不发 messages 帧。`nonempty` 断言会因此红——
            # 那是**真的**（主人确实没读到东西），不该由适配器补一段话来掩盖。
            yield ("updates", {"model": {"messages": [m]}})
            return
        yield ("messages", (AIMessageChunk(content=text), {"langgraph_node": "model"}))
        yield ("updates", {"model": {"messages": [m]}})

    def _on_wrap_up(self, m: AIMessage) -> Iterator[tuple[str, Any]]:
        """收敛中间件的确定性收尾 → 用户可见正文（见 `stream` 里那段注）。

        与 `_on_model` 分开只为一条判据：这一条**永远是最终答复**——中间件只在跳 `end`
        时注入它。所以即使它真带了工具调用也**不许**按决策轮处理（那会凭空多发一份
        `plan_obj`，而生产那一轮根本不会执行）。正文为空时不发帧：`nonempty` 断言因此
        红，那是**真的**（同 `_on_model` 的空收尾那条注）。
        """
        text = str(m.content or "")
        if not text:
            return
        yield ("messages", (AIMessageChunk(content=text), {"langgraph_node": "model"}))
        yield ("updates", {"model": {"messages": [m]}})

    def _on_tools(self, executor: SkillExecutor, ledger: RunLedger,
                  sent_calls: int) -> Iterator[tuple[str, Any]]:
        """内层的一次工具轮：真工具调用**已经发生**（executor 在包装层里跑完了）。

        顺序与生产同源：工具帧（`messages`）先到，`execute` 的 update 后到——
        `graph.execute_node` 也是先产 ToolMessage 再交回 update。
        """
        new = executor.calls[sent_calls:]
        for i, c in enumerate(new, start=sent_calls):
            yield ("messages", (_tool_frame(i, c), {}))
        # 回执是**累计语义**（生产 execute_node 每次交的都是请求内全部 PASS 行，
        # producer 靠 `receipt_sent` 下标算增量）⇒ 这里也必须给全量。
        yield ("updates", {"execute": {
            "receipts": list(ledger.receipts),
            "blocked": [_blocked_row(b) for b in ledger.blocked]}})
        return len(executor.calls)

    # ── 弹卡（P3） ────────────────────────────────────────────────────────
    def _pending_popup(self, ledger: RunLedger, executor: SkillExecutor,
                       state: dict, plan_obj: dict, messages: list, principal: Any,
                       user_msg: str, config: dict | None) -> dict | None:
        """同意闸上攒下的写 spec → 生产的确认卡（`graph._confirm_popup`，**同一个资产**）。

        「触发逻辑照搬」在这里的实现方式是**直接调用它**，不是抄一份：那里面的排序
        （免弹窗的三条前提 → 硬权限 → 参数/`$ref` → 文章目标有据 → 惰性快照 →
        `reached_specs` 滤空）改一处漏一处就是一条新漂移源，而它恰好是本仓最常见的
        那种缺陷。它能被这么用的理由是它只读 state 的四个键；`_popup_state` 按那四个
        键拼一份最小 state。`config` 原样透传：它要做 tag/cat/board/note/users/todos…
        那一串**惰性读**，配置缺了会静默退化成"读不到 ⇒ 只印名字"（方向安全，但读数
        会与生产不同，所以不自己另拼一份 config）。

        返回 `None` = 这批不该弹卡（主人这句是提问、参数里还挂着 `$ref`、目标不在
        主人原话里……都是 `_confirm_popup` 里的 `continue`）。此时这批 spec **既没
        执行也没卡**，与生产同形——生产那一轮它们会走逐 spec 的 `consent_frame` 错误帧。
        判过即清 `pending`：同一批 spec 不该被两张卡问两遍。
        """
        if not ledger.pending:
            return None
        specs = [str(p.get("spec") or "") for p in ledger.pending]
        ledger.pending.clear()
        return _confirm_popup(_popup_state(state, plan_obj, ledger, messages, executor),
                              specs, principal, user_msg, config)

    def _on_popup(self, popup: dict, ledger: RunLedger) -> Iterator[tuple[str, Any]]:
        """弹卡轮 / 零改动轮的帧（与 `graph.execute_node` 返回的那一格**同形状**）。

        正文**不在这儿发**：producer 自己读 `confirm_text` / `noop_text` 去 `emit_text`
        ——那是用户可见正文的"唯一出口"。这里再补一条 messages 帧会让同一段正文**入队
        两次**（`run_golden` 把两种帧都拼进 `text`）；生产那一轮根本到不了 model 节点，
        所以那边的正文只有一份。同理不发 `model` update：那一格在生产的弹卡轮里不存在。
        """
        kind = str(popup.get("kind") or "")
        if kind not in ("confirm", "noop"):
            # 认不出的判别键 = 生产端与消费端漂移，**响亮失败**（同 `execute_node` 那条）：
            # 宁可这一轮如实收尾，也不拿一个来路不明的 dict 当弹卡批次发出去。
            logger.error("[react_arm] 确认出口的判别键认不出（kind=%r，键=%s）→ 按零改动收尾",
                         kind, sorted(popup))
            yield ("updates", {"execute": {
                "noop_text": "这一步现在没有需要改动的地方，我就没有动手。",
                "noop_note": f"确认出口判别键认不出（{kind or '空'}），本轮零改动",
                "messages": [], "receipts": list(ledger.receipts)}})
            return
        body = {k: v for k, v in popup.items() if k != "kind"}
        yield ("updates", {"execute": dict(body, messages=[],
                                           receipts=list(ledger.receipts))})

    # ── 闸门（P4，20261011）──────────────────────────────────────────────
    def _gate(self, reply: AIMessage, plan_obj: dict, ledger: RunLedger, state: dict,
              messages: list, executor: SkillExecutor, config: dict | None,
              replan_used: bool) -> dict:
        """最终答复候选过一遍 `graph.gate_node`（**同一个资产，不抄第二份**）。

        **为什么必须直调它**：这条臂此前结构上不会发 fallback ⇒ `arm_capability`
        判 `comparable=False`（138 条 `forbid_fallback` 全白发），那 138 条断言连同
        它们保护的"不许把没做的事说成做了"一起失效。判据住在 `gate_node` 里
        （`_REPLAN_ISSUES` 约 20 族 / `_claim_issue` 的声称族 / `_repeat_reason` 的
        复读族 / 编造 URL / 零帧如实核验），**一条也不许在这里重写**——抄一份就是
        抄一条漂移源，而"人工同步 94 处"正是本仓架构审计的头号结论。

        **重规划只此一次**，分两处保证，与生产同源：
        · 本方法把上一轮的 `replan_used` 透进 `gate_replan` 键 ⇒ `gate_node` 的
          `fail()` 走 `_fallback_result`（它自己就有那条 `not state.get("gate_replan")`
          的判据，见 `graph.py` 的 `fail` 闭包）；
        · 外层 `stream` 的两圈封顶。

        返回三种之一：`{"kind": "pass"}` / `{"kind": "fallback", "text": …}` /
        `{"kind": "replan", "messages": [...]}`。**不在这里发帧**——翻译成帧的那一步
        在 `stream` 里（两条通道各发什么，那里有注）。

        异常**吞掉并按 pass 处理**（`logger.warning` 留痕）：闸门是**加装**的一层，
        它自己炸了不该把主人这一轮的答复整条吃掉——生产那边 `gate_node` 抛异常会被
        langgraph 变成整轮错误，本线没有那层兜底，所以这里自己兜。
        """
        try:
            out = gate_node(
                _gate_state(state, plan_obj, ledger, messages, executor, reply,
                            replan=replan_used),
                config)
        except Exception as e:  # noqa: BLE001 —— 见 docstring 最后一段
            logger.warning("[react_arm] gate_node 抛异常，本轮按「未打回」处理：%s", e,
                           exc_info=True)
            return {"kind": "pass"}
        if out.get("gate_replan"):
            # 只留那条提示，**丢掉 `RemoveMessage`**：`_replan_result` 拿它把被否定的
            # 那条 AI 消息从生产 state 里摘掉，而本线内层每次 invoke 都从空 state 起
            # ——那条消息**天然不在**上下文里（与生产同形），照搬 RemoveMessage 反而
            # 会把它塞进下一次 invoke 的输入消息表（那是 `add_messages` 的指令、不是
            # 消息，混进 `create_agent` 的入参是未定义行为）。
            return {"kind": "replan",
                    "messages": [m for m in (out.get("messages") or [])
                                 if isinstance(m, SystemMessage)]}
        text = str(out.get("fallback_text") or "")
        return {"kind": "fallback", "text": text} if text else {"kind": "pass"}

    def _crash_tail(self, ledger: RunLedger, err: Exception
                    ) -> Iterator[tuple[str, Any]]:
        """内层异常时的确定性收尾（只说系统确认过的事实，不声称任何动作）。"""
        done = [str(r.get("tool") or "") for r in ledger.receipts]
        if done:
            text = (f"我停在这里了：内部出了点问题（{type(err).__name__}），这一轮没法继续。"
                    f"此前确实办成 {len(done)} 件（{'、'.join(done)}），剩下的你再说一声。")
        else:
            text = (f"我停在这里了：内部出了点问题（{type(err).__name__}），"
                    "这一轮没有任何一件办成，我没有假装办好了。")
        m = AIMessage(content=text)
        yield ("messages", (AIMessageChunk(content=text), {"langgraph_node": "model"}))
        yield ("updates", {"model": {"messages": [m]}})


# ── 建臂 ─────────────────────────────────────────────────────────────
def build() -> ReactGoldenArm:
    """`golden_arm.build_agent()` 的入口（约定名，改名要连着改那边）。"""
    return ReactGoldenArm()


def _llm() -> Any:
    """决策用 LLM：**超时直读 settings，额度与思考是这一条臂自己的档**。

    ⚠️ **温度那一格是手抄的 0.2，与生产的 `settings.planner_temperature`（0.0）不同。**
    这不是本批引入的（`graph.planner_llm` 在 `graph.py:6918` 读 settings，本线从建立起就
    写死 0.2），但**别把它当成"同组"**——0.2 正是生产 20261006 之前的那一档。**不动它**：
    改一个字就是改行为，而"温度能治路由抖动"这个归因已被两臂交替的 A/B 证伪
    （`docs/param-tuning-20261006.md`），没有理由把一次未验证的行为改动塞进一个宣称
    "只改延迟"的批次。真要统一到 `settings.planner_temperature`，**单独一批 + A/B**。

    20261013 批 A 把额度与思考从 `planner_native_*` 上摘下来，理由是**形态不同**：
    `planner_native_max_tokens` 那个 8192（产线 `.env`）是"**一次吐完整计划**"的档，
    而本臂里一次决策要么是一个工具调用、要么是一句收尾——**那个形态已经不存在**。
    不摘的账单是 trace `20261013T095258_1_rde4e103`：一次决策吐满 **30,540 字符**、
    `finish=length` 被截断、兜底重跑一遍，两次合起来 **70 秒**（整轮 78 秒）。
    `enable_thinking=False` 同 `_narrator_llm` 那条理由（长上下文下思考链爆炸，
    20260830 超时事故），本线的决策上下文只比 narrator 更长。

    ⚠️ **这一改动了"两臂同解码"那条不变式**，是有意的：两臂的读数本来就**不可互比**
    （收尾由谁写、判据判哪一段都不同），本批之后的对照一律在**同一条臂的改前改后**
    之间做。**不许**为了"让两臂对齐"去改 `planner_native_max_tokens`——graph 臂还要
    逐字用它，基线不能自己动。
    """
    return get_llm(temperature=0.2,
                   max_tokens=settings.react_decision_max_tokens,
                   timeout=settings.planner_native_timeout,
                   enable_thinking=False)


def _narrator_llm() -> Any:
    """narrator 用 LLM：**与生产 `model_node` 同一句**（`get_llm(enable_thinking=False)`）。

    与 `_llm()`（决策那份，走自己的额度档 + 直读 `planner_native_timeout`）**故意不共用**：生产就是
    两个不同的参数档（决策要低温度短输出，叙述要能长篇）。共用一个工厂就等于把 P6 刚拆开的
    两件事又捏回一个模型——那时"闸门判的对象与生产同不同种"这个问题会重新变成"看运气"。

    `enable_thinking=False` 的理由与生产那条注同源（长上下文下思考链爆炸，20260830 超时
    事故），本线的 narrator 上下文比生产更长：整份技能表 + 循环说明 + 工具帧。
    """
    return get_llm(enable_thinking=False)


def _narrator_prompt(rst: dict, receipts: list, drop: set[str], config: dict | None,
                     role: str | None) -> str:
    """生产 narrator 的提示词资产，**逐字同一份**（`graph._EXECUTOR_PROMPT`）。

    本函数只负责"哪几个格填什么"，三个格的口径与 `graph.model_node` 逐条对齐——
    凡是与它不同的地方都是**明写的口径差**，不是随手：

      · `plan=_narrator_plan(rst, config)`：计划文本 + 账甲那条本轮事实。传的是 `rst`
        这份最小 state（不是生产的 `AgentState`），而 `_narrator_plan` 只读
        `plan`/`receipts`/`noop_specs`/`blocked`/`blocked_seen`/`pending_confirm`/
        `ledger`/`confirm_grant`——`_round_state` 给的正是这一批，**与闸门读的是同一份**
        （两个消费端不会对同一轮算出不一样的形状）；
      · `tool_frames` 走 `article_pointer=True`：生产只有 narrator 这一处这么用（原始
        ToolMessage 就在它手边的消息流里，未超预算的正文帧两份逐字节相同 ⇒ 换指针句
        省掉整整一份正文）；
      · `fact_block` 按 `is_action_family` 分岔，判据是**回执**不是"印了几行"（见
        `_NO_PRINTED_FACTS` 的注：20261002 那次"系统发的空授权"就是按错判据分岔的现场）。
    """
    msgs = list(rst.get("messages") or [])
    return _EXECUTOR_PROMPT.format(
        persona=BLOG_ASSISTANT_PROMPT,
        audience=audience_block(role),
        plan=_narrator_plan(rst, config),
        tool_frames=_frame_texts(msgs, drop_tools=drop, article_pointer=True),
        exec_receipts=_receipts_text(receipts, drop_tools=drop),
        fact_block=block_of(receipts) or (_NO_PRINTED_FACTS_ACTION
                                          if any(is_action_family(r) for r in receipts)
                                          else _NO_PRINTED_FACTS),
        page_ctx=_page_ctx(msgs, role),
        sticker_guide=STICKER_GUIDE)


def _narrator_call(msgs: list) -> AIMessage | None:
    """零工具调用一次，把生产 narrator 那两段补救**照原样**带上。

    两段都是真事故换来的，所以这里一条不少地复用同一套判据与同一份措辞资产：
      · **空内容重试一次**（20261001）：上游偶发"有 completion token、正文却是空串"
        （133 个 narrator 轮里 3 条），重试是"再问一次模型"，比放宽判据更接近事实；
      · **回了 `tool_calls` 的纠正**（20261006）：零工具节点最坏的形态不是报错，而是
        "只写了一句开场白 + 一条 tool_call"——正文非空 ⇒ 闸门判 PASS ⇒ 主人收到一句
        内容为零的话却当成功交付。`_NARRATOR_NO_TOOLCALL_NUDGE` 是那一批的资产。

    返回 `None` = 两次都没拿到可用正文，调用方退回决策模型那条草稿：一条有内容、没经过
    narrator 资产的话，仍然比"我刚才好像卡住了"诚实。
    """
    llm = _narrator_llm()
    resp = llm.invoke(msgs)
    if not str(getattr(resp, "content", "") or "").strip():
        logger.warning("[react_arm] narrator 返回空内容 → 重试一次")
        resp = llm.invoke(msgs)
    if getattr(resp, "tool_calls", None):
        _bad = resp
        logger.warning("[react_arm] narrator 回了 tool_calls（零工具节点不该有）n=%d"
                       " → 带纠正重问一次", len(_bad.tool_calls or []))
        _corr = llm.invoke(msgs + [SystemMessage(content=_NARRATOR_NO_TOOLCALL_NUDGE)])
        resp = (_corr if not getattr(_corr, "tool_calls", None)
                and str(getattr(_corr, "content", "") or "").strip() else _bad)
    text = repair_sticker_tokens(str(getattr(resp, "content", "") or ""))
    return AIMessage(content=text) if text.strip() else None


def _skill_menu(role: str | None) -> list[StructuredTool]:
    """技能级菜单：与 planner 看到的 schema **逐字同一份**（`build_tool_schema`）。

    `func` 只是占位——`wrap_tools_with_receipts` 会把每个工具重建成同名的新
    StructuredTool，真正被调的是那一层（它转给 `SkillExecutor`）。占位体故意抛异常：
    万一将来包装层没接上，症状是**响亮的一次调用失败**，而不是"调了个什么都不做的桩"
    （后者会让整轮看起来跑通了、其实一个工具都没执行）。
    """
    out: list[StructuredTool] = []
    task_state = bool(getattr(settings, "agent_task_state", False))
    for item in build_tool_schema(role, task_state=task_state):
        fn = (item.get("function") or item) if isinstance(item, dict) else {}
        name = str(fn.get("name") or "")
        if not name:
            continue
        out.append(StructuredTool(
            name=name, description=fn.get("description") or "",
            args_schema=fn.get("parameters") or {"type": "object", "properties": {}},
            func=(lambda _n=name, **_k: (_ for _ in ()).throw(
                RuntimeError(f"技能菜单占位体被直接调用（{_n}）——包装层没接上")))))
    return out


def _real_tools(config: dict | None = None) -> dict[str, Any]:
    """真工具名 → 可调用体。**必须是纯函数签名（`**kwargs`）**：

    `SkillExecutor` 是 `fn(**args)` 调的，而 `BaseTool.__call__` 的第一个位置参数是
    `tool_input` ⇒ 直接塞 `StructuredTool` 会把它当成"参数名恰好叫 path 的 tool_input"，
    症状是**被包装层兜成错误帧**（不炸图），看起来像"每个工具都 BLOCK error_frame"
    ——判据在开火，其实是适配器写错了。这里统一走 `.invoke(单个 dict)`，与生产
    `graph.execute_node` 的调用形态一致。

    ⚠️ **`config` 必须透传**（20261005 修）：生产方式（`server.py:975`）把
    `configurable.user_id` / `principal` / `conversation_id` 注进 config，声明了
    `config: RunnableConfig` 的工具（`list_devices`、`list_my_messages`、所有
    `_device_get_user_id` 的消费方）全部从那里读身份。不传 ⇒ 那些工具**永远**看到
    uid=0，无论请求带没带身份 —— 症状是"带真身份的用例集体红"，而红的原因住在适配器里，
    报告上却长得像模型不会用工具（这是**假阴性**，与能力台账要防的假阳性是同一个病的
    两面）。golden 的 127 条可比子集里没有带身份的用例（那一族要 `GOLDEN_*_UID`），
    所以这一条今天不改变任何读数——它修的是"带身份那一族接入时不会凭空红一片"。
    """
    cfg = dict(config or {})
    return {t.name: (lambda _t=t, **kw: _t.invoke(kw, config=cfg)) for t in get_all_tools()}


def _system_prompt(messages: list, role: str | None, principal: Any,
                   config: dict | None, user_msg: str,
                   fast_frames: list | None = None) -> str:
    """系统提示 = 人设 + 受众 + 贴纸名字表 + **循环机制** + 事实注入。

    **20261013（批次 B）：规划契约整块摘掉。** 此前第一块逐字拼的是
    `graph._render_planner_prompt(...)`——技能表、判定规则 1..7、输出契约，
    实测 admin 档 **25,121 字符 / 占整份提示词 70%**。那份文本是为"一次吐完整计划"
    的**解析器**写的（`SKILL=`/`PARAMS=` 契约行、`TODO:` 行、PARAMS.calls 里点名工具、
    每轮重渲染的轮次说明）：本循环里模型**直接调函数**（技能表已经在函数 schema 里），
    决策由工具调用表达——那些话一句都不再需要它读。删掉之后提示词从 **35,762 字符降到
    4,936**（admin 档；user 档 28,119 → 4,474；两次都同一份输入、同一个入口实测）。
    拆开看：人设 1,979 + 受众 542 + 贴纸名表 537 + 循环机制 1,423 + 六格事实的小标题
    362 + 各格的值。**契约指纹全零**：`SKILL=` / `PARAMS=` / `TODO:` / `输出契约`
    在这些渲染里各出现 0 次。

    ⚠️ **这一批单独跑就掉一档，掉的那条恰好指认了下一批要补的洞**（20261013 实测，
    两臂交替各 4 遍）：`multi_step_search_then_read_top` 改前 4/4、改后 **0/4**，
    失败形态三样——零工具调用且正文自称"本轮还没检索"、检到了不读全文、拿
    `list_notes` 顶替检索工具。顺着读下去，原因不在循环机制那几行字，而在**工具面**：
    `content_query.tools` 那格枚举列的是 26 件**无参只读**工具（`list_guestbook` /
    `get_top_notes` / `list_admin_notes` …），**`search_notes` / `rag_search` /
    `list_notes` / `get_article_detail` 一个都不在里面**；检索只能走
    `calls=[{tool, args}]`，而那一格**没有枚举、也没有任何一句工具说明**（实测
    `items.description` 是 None）。删掉的那份文本菜单（67 件工具的中文说明）和契约
    规则 3③ 是"该点哪一个"的**唯一**住处——技能面确实一直在替契约干活。
    ⇒ 照计划进**批次 C**（把工具面按"模型能不能拿准"重排），**不回退本批**。
    另一条读数同批留给 C：技能自己那份 `description` 里还留着指向已删契约的话
    （`content_query` 写"见规划规则 6"、`article_status` 写"见 planner 契约"），
    在函数菜单里已经是**悬空引用**。

    **`NARRATOR_DISCIPLINE` 与它的立场改写也一起摘掉**（同批）：叙述纪律是给
    **零工具节点**写的（第 1 条"你没有任何可以直接调用的工具"），而 20261012 起本臂的
    收尾正文由**独立的零工具 narrator** 写（`_narrate` → `_EXECUTOR_PROMPT`，
    那份资产里本来就带着这份纪律，**一个字没动**）。决策模型既不再写正文，那条
    纪律与它的立场改写就是两段没人受益的文本。

    **留下的每一块都还在干活**：
    · `BLOG_ASSISTANT_PROMPT`（人设 + 叙述边界 + 诚实底线）；
    · `audience_block(role)`（对谁说话）；
    · `STICKER_GUIDE`（情绪贴纸的**唯一**名字表——不接它 `:害羞:` 这类记号结构上
      产不出来，`sticker_*` 用例整族恒红，而语料只认这 12 个名字）；
    · `_LOOP_MECHANICS`（这个循环怎么走、怎么结束）；
    · `_facts_block(...)`（页面上下文/已点名文档/最近对话/短应答提示/待办台账/工具帧
      ——**这一格必须留**：`record("planner","ledger_frame", …)` 是三条
      `require_ledger_*` 判据的唯一证据，而 tracer 认的是这个事件名）。
    """
    try:
        ledger_frame, _meta = _pending_ledger_frame(
            user_msg, _last_assistant_utterance(messages), principal, config)
    except Exception:  # noqa: BLE001 —— 台账读不到不该炸掉整轮（它只是背景事实）
        logger.warning("[react_arm] 待办台账读取失败，按「没有台账」处理", exc_info=True)
        ledger_frame, _meta = "", {}
    if ledger_frame:
        # 台账帧**进了这一轮的提示词**这件事必须留痕（事件名与形状逐字同
        # `graph.py` 的 planner：`record("planner", "ledger_frame", **meta)`）。
        #
        # 为什么非记不可：`require_ledger_frame` / `require_ledger_rows` /
        # `require_card_targets_from_ledger` 三条判据读的**唯一证据**就是这个 trace
        # 事件（`run_golden` 从 `planner.ledger_frame` 取 `ids`/`rows`）。本线此前
        # 把台账读进了提示词却一个字都没记 ⇒ 那三条判据在本线上**恒假**：20261012
        # 全量双臂里 `admin_board_audit_reviewed_refusal` 因此报红，而两臂那一轮的
        # 行为**逐字相同**（回复正文一字不差、确认卡载荷的 skill/tool/args 全同）
        # ——红的是证据通道，不是行为。这与 `forbid_fallback` 那 138 条是同一个病的
        # 两个方向：那边是"做不到 ⇒ 纯否定断言恒真"（假绿），这边是"不产证据 ⇒
        # 要求型断言恒假"（假红）。
        #
        # 条件与生产同源（`graph.py` 的 `if rounds == 0 and ledger_frame`）：本线的
        # 提示词每请求只建一次，等价于生产那个 `rounds == 0`；空帧不记——"没摆上桌"
        # 与"摆了张空的"在那三条判据里是两件事（`require_ledger_frame` 明写「只判摆
        # 没摆，不管队列空不空」，而生产连空帧都不记 ⇒ 本线照抄，别自作聪明放宽）。
        record("planner", "ledger_frame", **_meta)
    return "\n\n".join([BLOG_ASSISTANT_PROMPT or "", audience_block(role),
                        STICKER_GUIDE, _LOOP_MECHANICS,
                        _facts_block(messages, role, ledger_frame, fast_frames)])


def _facts_block(messages: list, role: str | None, ledger_frame: str,
                 fast_frames: list | None) -> str:
    """本轮的**事实格**（页面上下文 / 已点名文档 / 最近对话 / 短应答 / 待办台账 / 工具帧）。

    **每一格都调 helper 取值，一个字不手写**：这些措辞（"（本轮尚无工具执行）"、
    "（本会话还没有点名的文档）"…）在生产那边是 planner 提示词模板的一部分，`_PLANNER_PROMPT`
    改了本适配器要跟着改的一份漂移源，所以这里只写**小标题**，值一律来自同一批 helper
    ——与 `graph._render_planner_prompt` 取的是同一份值。小标题是必要的：helper 的返回值
    **不带任何标题**（`_page_ctx` 直接是一串 `current_url=…`），而这几格只靠内容自己
    说不清自己是哪一格。

    **为什么摆在提示词末尾**：本轮的每一格都是"这一请求的事实"，人设与循环机制是
    "永远不变的那一段"——不变的在前、逐轮变的在后，前缀缓存才吃得住（这条臂此前
    因为纠偏文本插进前缀把 `cache_read` 从 38400 打到 4992，见计划 §五）。

    **`fast_frames` 这一格必须留**（20261005）：当前文章读取快道那一次读取发生在内层循环
    **开始之前**，消息流里不会有它的 ToolMessage（`_on_tools` 只发内层循环产生的增量）
    ⇒ 不摆在这里，模型就永远看不到它刚读到的那篇文章。渲染走 `_frame_texts`
    （`get_article_detail` 是全文帧，那里有放宽的截断预算），不手写摘要。
    """
    return "\n\n".join([
        _HEAD_PAGE_CTX, _page_ctx(messages, role),
        _HEAD_DOC_ANCHORS, _doc_anchors(messages),
        _HEAD_RECENT, _recent_tail(messages),
        _HEAD_SHORT, _short_reply_hint(messages),
        _HEAD_LEDGER, ledger_frame or "（本轮没有去读待办台账）",
        _HEAD_FRAMES, _frame_texts(list(fast_frames or [])),
    ])


# ── 事实格的小标题 ───────────────────────────────────────────────────
# **只写标题，值一律来自 helper**（见 `_facts_block`）。helper 的返回值**不带标题**
# （`_page_ctx` 直接是一串 `current_url=… ; current_darkmode=…`），所以这几行是必要的：
# 不给标题，六格内容连成一坨。
#
# 标题里那半句"以此为准 / 别按对话历史猜"不是装饰——它防的那个错**工具返回防不住**
# （工具根本不知道主人此刻看着哪一页），20260925 生产实证过模型拿对话历史里的旧位置
# 当"此刻在哪一页"，据此调了错的工具。按计划 §二那条判据过了一遍，这一类留下。
_HEAD_PAGE_CTX = """\
当前页面上下文（前端实时上报的事实——访客此刻在哪一页、什么特效、夜间模式开没开，
以此为准，**不要**凭对话历史推断位置）："""

_HEAD_DOC_ANCHORS = """\
本会话已点名文档（系统从对话历史与跨轮执行记忆里确定性提取的指代锚点——判"主人说的
是哪一篇"时**先在这里对号入座**，在列的不必再检索；`noteId` 是**文章 id**，别拿它去
调别的工具）："""

_HEAD_RECENT = "最近对话节选："

_HEAD_SHORT = """\
短应答提示（主人这句话只是"好/不用了/你看着办"这类短应答时，这里给出它承接的上一轮
发言与判定方向；不是短应答则为缺省语）："""

_HEAD_LEDGER = """\
待办台账（**系统现场读的后台队列**：等着主人点头的那几件，逐条带 id。它是**事实**，
不是纪律——办不办、办哪几件由你决定；这一轮没去读它时这里是缺省语）："""

_HEAD_FRAMES = """\
本轮已执行工具的结果（系统在你调用后立刻执行；真实返回既在这一格，也在消息流里）："""


# ── 循环机制 ─────────────────────────────────────────────────────────
# 把"这个循环怎么走、怎么结束"讲清楚。**不写叙述纪律**——那是 narrator 的活
# （`_EXECUTOR_PROMPT` 里那份，逐字没动），20261013 起本臂不再带第二份。
#
# 这段由 `_CLOSING_RULES`（怎么收尾）与 `_ROUND_INFO`（这是自由循环、可以连着调）
# 长成：批次 B 把规划契约整块摘掉之后，原来由 planner 规则 1..7 承担的那几句
# ——"一轮挑几件""回执说没办成怎么办""目标不确定怎么办"——得有人接手。
#
# ⚠️ 第 3 条那句是**这一批里唯一改了事实口径的一句**：P6（20261012）起，收尾正文由
# 独立的零工具 narrator 按工具帧重写，决策模型那条零工具消息只是"我这边够了"的
# **信号**（narrator 两次都拿不出正文时才拿它兜底）。不写明这件事，提示词就在骗
# 模型——它会以为自己写的就是主人要读的那段，于是按"写作文"的标准去写、把没执行的
# 事讲成执行了。写明之后它只需要如实收束。
#
# ⚠️ **第 7、8 条是 A/B 读出来的补丁，不是凭空加的**（20261013 删契约之后：
#   · `multi_step_search_then_read_top` 新臂 **0/4**、旧臂 4/4：模型用 `content_query`
#     连发两次检索（`search_notes`+`rag_search`、又一遍 `rag_search`）却**一次全文都没
#     读**，中间件按"主人要的那件事没做"纠偏，它再检索一遍 ⇒ 撞 `REC_LIMIT=24`
#     （`GraphRecursionError`）。契约里那条"定位到之后要读/要跳"是它唯一的出处；
#   · `mt2_short_yes_resubmits_card` 新臂 **2/4**、旧臂 4/4：第 2 轮主人点头，模型只
#     叙述"那件事还挂着等你点头"、**零控制帧**——契约里"系统给了 pending_action 时
#     那件事就是它、照原样重新提交"是它唯一的出处。
# 两条都不是"工具返回能教会的事"：一件根本没调工具，另一件调错了工具、回执不会替它
# 想起主人要读全文。所以按计划 §二那条判据，它们留在**本臂自己的循环说明**里，
# 而不是回到 planner 那份契约（契约整块摘掉是这一批的本体，回退＝这一批白做）。
# **参数与安全一个字没放宽**：第 8 条只说"原样重提"，同意闸照样拦、卡照样弹。
_LOOP_MECHANICS = """\
循环机制（你既是决策者也是回复者，两者同体。本提示词每请求只渲染一次，下面说的"轮"
只有系统在数——你不必知道自己第几轮，只管一件事一件事往下办）：

1. **一轮挑一件。** 要做事就直接调用工具——**你的工具调用就是执行本身**（本轮的函数
   菜单就是你的全部本领），系统会立刻执行、把真实结果作为一条工具消息还给你。
   ⚠️ 前面人设里那句"站内操作都由系统执行，你的回复正文只是叙述、不会触发任何执行"
   是**对正文**说的：正文里写任何像命令的东西都不会生效（所以正文里一个都别写）；
   但**工具调用不是正文**。站里的事你有工具能查、能办——别把自己当"只能叙述、查不了"
   的角色，更**不许拿"我还没查过"当答复**（那等于什么都没做：先调工具，看回执再说话）。
2. **看回执，再决定下一步。** 回执说没办成，就换参数、换工具或换个说法（写着"未生效"
   的同一条**别原样重试**）；一件事办妥了再动下一件。主人一口气交办了几件，就在接下来
   的几轮里一件件办完——**别只办成一件就收尾**。
3. **信息已经够回答主人了 ⇒ 交出一条不带任何工具调用的消息**，这一轮到此为止。那条
   消息是**交给系统的收束信号**（系统据此组织给主人看的回复，它也是万不得已时的兜底），
   所以：可以写得简短，但**必须属实**——里面不许有任何你在回执、事实格或主人原话里
   找不出出处的执行声称。
4. **工具回执说"目标对不上/不确定是哪一个" ⇒ 把候选念给主人**让他认，别自己硬挑一个
   就动手。这类回执里通常已经把候选列给你了。
5. 调用 `chat` 是**闲聊兜底**（把答复文本填进 reply）：它不产生任何执行事实，纯问候、
   情感交流、不需要查任何东西的问答走它；能用一句正常的最终答复说清的事，别绕它。
6. **复核**：说出口的每一条站内事实，都要能在工具回执、事实格或主人原话里找到出处，
   找不到就如实说没查到——**读不到 ≠ 空**、没读 ≠ 没有。站里的事你有工具能查，
   **不要**把"你先去登录""你自己去后台看看"这类活派回给主人。
7. **检索只是第一步，不等于回答。** 主人要的是"某篇里写了什么 / 把那篇读给我看看"时，
   检索帧给的是**候选**：拿到对得上的那一篇的**真实 id 之后还要再读全文**，只把标题念
   一遍不算回答。限定词与候选标题对不上就别硬拿第一条顶上，如实说候选里没有对得上的
   那篇。**同一句主题词反复检索＝没在推进**（检索按相似度排序，第二次不会比第一次准）。
8. **主人对上一轮的提议点头**（"嗯 / 好 / 就按刚才说的办"）**不是新话题**，是同意：
   把上一件**原样再提一次**——同一个技能、同一套参数**照抄**，不换目标、也不许缩水成
   "我知道了"。页面上下文里若有 `pending_action=` 那一行（上一轮提出、正等主人点头的
   那件写操作），"那件事"就是它。系统会因此再弹一张卡；只回一句"那件事还挂着等你点头"
   等于没接住。

工具回执与末尾那几格事实是**同一等**的来源：消息流里的 ToolMessage 是你自己调出来的，
末尾几格是系统注入的。两者都找不到出处的事，不许说成有。"""




# ── plan_obj / blocked 行 ────────────────────────────────────────────
def _plan_of(tool_calls: list, role: str | None) -> dict:
    """模型这一轮点的技能调用 → 生产的 `plan_obj`（producer 只读 `skill` 与 `tools`）。

    一轮多条调用时合并成一份：`skill` 用 `|` 连起来，`tools` 取并集——生产
    `parallel_tool_calls=False` 保证它那边恒为一条，这里是有意的容差（见模块头注的
    口径偏差）。`instantiate_plan` 是纯函数，这里算一遍、包装层执行时还会再算一遍
    （两次同参同果；不共享是因为执行那一步在 `SkillExecutor` 里，改它会让本线多一个
    "为了评测而存在的"耦合）。
    """
    plans: list[dict] = []
    for c in tool_calls:
        name = str((c or {}).get("name") or "")
        args = dict((c or {}).get("args") or {})
        try:
            plans.append(dict(instantiate_plan(name, args, role)))
        except Exception:  # noqa: BLE001 —— 实例化失败不该让整轮消失
            logger.warning("[react_arm] instantiate_plan 失败：skill=%s args=%s", name, args,
                           exc_info=True)
            plans.append({"skill": name, "tools": [], "note": "", "reply": "",
                          "status": ""})
    if not plans:
        return {}
    if len(plans) == 1:
        return plans[0]
    return {"skill": "|".join(str(p.get("skill") or "") for p in plans),
            "tools": [t for p in plans for t in (p.get("tools") or [])],
            "note": "", "reply": "", "status": ""}


def _popup_state(state: dict, plan_obj: dict, ledger: RunLedger, messages: list,
                 executor: SkillExecutor) -> dict:
    """喂给 `graph._confirm_popup` 的最小 state（它读的四个键，见 `_pending_popup`）。

    `messages` 必须带上**本轮已执行工具的真结果**：`_target_evidence`（"文章 id 有据"
    那条判据）从 `ToolMessage` 里找材料，喂空的会让文章族写操作的目标判据恒假 ⇒ 卡弹不
    出来——方向是**假阴性**，恰好是"评测里做不到生产做得到的事"，而报告上看起来像
    "这臂不会弹卡"。`executor.calls` 就是这一轮的逐次真调用（工具名 + 结果原文）。
    """
    calls = list(getattr(executor, "calls", []) or [])
    extra = [ToolMessage(content=str(c.get("result") or ""),
                         name=str(c.get("tool") or ""),
                         tool_call_id=f"popup_{i}") for i, c in enumerate(calls)]
    return {
        "messages": list(messages or []) + extra,
        # `_plan_skill` 只读这一格，而令牌里签的技能名就是它：空 ⇒ `sign` 拒绝签发
        # ⇒ 卡弹不出来（所以 `stream` 里按"最近一次有效计划"记着，见那里的注）。
        "plan_obj": dict(plan_obj or {}),
        "receipts": list(ledger.receipts),
        "confirm_grant": (state or {}).get("confirm_grant"),
    }


def _plan_for_gate(plan_obj: dict) -> dict:
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


def _gate_state(state: dict, plan_obj: dict, ledger: RunLedger, messages: list,
                executor: SkillExecutor, reply: AIMessage, *, replan: bool) -> dict:
    """喂给 `graph.gate_node` 的最小 state——**它读的键，逐个对得上**（20261011）。
    P6 起它只是 `_round_state(..., reply=…)` 的一层壳（键清单与理由都在那边）。"""
    return _round_state(state, plan_obj, ledger, messages, executor, reply=reply,
                        replan=replan)


def _round_state(state: dict, plan_obj: dict, ledger: RunLedger, messages: list,
                 executor: SkillExecutor, reply: AIMessage | None = None,
                 *, replan: bool = False) -> dict:
    """本轮的最小 state——**两个消费端读的是同一批键**（20261011 起 gate，20261012 起 narrator）。

    `reply=None`（narrator 那一支）**不放答复**：生产 narrator 看不到任何"草稿"（它读的是
    计划契约与工具帧），把决策模型那条消息塞进去就是把 P6 拆掉的那件事又接回去。
    闸门那一支必须带（`_repeat_reason` 的复读比对要看得见它）。

    做法与 `_popup_state` 同族（那也是按目标函数读的键拼一份最小 state 去直调生产资产），
    但**这两处合成了一份**（20261012）：`_narrator_plan` 与 `gate_node` 读的是同一批键
    ——账甲 `_round_facts` 那一族正是两个消费端共用的合成函数，"两边各拼一份 state"
    必然在某一天让它们对**同一轮**算出不一样的形状。键清单是用 grep 从 `gate_node`
    **函数体**里数出来的（`state.get("…")` / `state["…"]`），不是凭记忆写的：

        messages / plan / receipts / ledger / pending_confirm / noop_specs / gate_replan

    ⚠️ **`gate_node` 的函数体里只有这七个是直接读的**（20261011 实测：`awk` 切出
    12345–12802 逐行 grep）。另外三个是它调用的**辅助函数**读的，函数体里看不见，
    20261011 补：

        blocked / blocked_seen  ← `_round_facts`（账甲）与 `_blocked_article_targets`
        confirm_grant           ← `_round_facts` 的 `granted`

    漏这三个**不会报错**，只会让闸门**少开火**（`.get(…, [])` 把"本线没喂"与
    "本轮确实没有"读成同一件事）——正是本仓最熟的"判据没接到底"那一族。两处已实证
    的影响面：`_claim_issue` 的洞⑥（确认话术声称）读 `facts.consent_blocked`
    ⇒ 缺 `blocked_seen` 时它恒 False ⇒ `promise_ok=True` ⇒ 那一族在本线上**不判**；
    `_blocked_article_targets` 读 `state["blocked"]` 的 `spec` ⇒ 缺它时 5a 的混合轮
    收窄恒不启用。`blocked` 的键形（`spec`/`tool`/`reason`）由 `_blocked_row` 给，
    与 producer 读的那一份**同源**——两处各拼一份就会一处在判、一处在显示。

    两处取法要**说准**，它们各自都有一个假读数：

    · `messages` **必须把本轮的工具帧放进去**——`frames` 是 `gate_node` 判"有没有执行
      事实"的唯一依据（它从消息流里挑 `ToolMessage`），喂空的会让**每一族声称判据**都
      判成"零帧轮"（方向是**过严**：一条真办成了事的回复会被当成零帧轮按更宽的族去查）。
      过闸门那一支还要把**答复本身**放进去（`reply=`），`_repeat_reason` 的复读比对才
      看得见它；narrator 那一支不带（见上）。
      帧用 `_tool_frame` ——**与发到用户/report 的是同一份**：闸门判的对象必须是主人
      真看到的那些帧，另造一份措辞不同的一次性帧＝判据与事实分家。
    · `plan` 走 **`plan_state(...)["plan"]`**（不是 `plan_encode` 直调 —— 那个函数
      有源码锁，只许 `plan_state` 调；也不是自己拼 `SKILL=` 行 —— 那是第二份排版）。
    · `ledger` 原样透传（洞⑦ 的台账否认判据读它，`graph_input` 装的就是这一格）。
    · `gate_replan` 是**上一圈有没有重规划过**，由调用方给（见 `_gate` 的注）。
    """
    frames = [_tool_frame(i, c) for i, c in enumerate(executor.calls)]
    blocked = [_blocked_row(b) for b in ledger.blocked]
    return {
        "messages": (list(messages or []) + frames
                     + ([reply] if reply is not None else [])),
        **plan_state(_plan_for_gate(plan_obj)),
        "receipts": list(ledger.receipts),
        # 账甲（`_round_facts`）与 `_blocked_article_targets` 读的三格：见 docstring
        # 里那段"辅助函数读的键"。`blocked_seen` 的键形与 `execute_node` 的
        # `_blocked_key` 逐字同款（`工具::原因码`）——生产那一支是**跨轮累积**，本线的
        # `ledger.blocked` 同样只增不减，所以这一次运行的范围就是同一种累积。
        "blocked": blocked,
        "blocked_seen": sorted({f"{b.get('tool')}::{b.get('reason')}"
                                for b in ledger.blocked}),
        "confirm_grant": (state or {}).get("confirm_grant"),
        "ledger": (state or {}).get("ledger") or {},
        "pending_confirm": (state or {}).get("pending_confirm"),
        "noop_specs": (state or {}).get("noop_specs") or [],
        "gate_replan": bool(replan),
    }


def _blocked_row(b: dict) -> dict:
    """受阻台账 → producer 读的那一行（它读 `spec` 与 `reason`）。

    `spec` 的形态与生产一致（`<工具>(<json 参数>)`），producer 用 `_spec_one` 解回来
    再渲染人话动作（`tool_action_text`）——自己拼中文动作行会让过程行两套措辞。
    """
    try:
        args = json.dumps(b.get("args") or {}, ensure_ascii=False)
    except Exception:  # noqa: BLE001
        args = "{}"
    return {"spec": f"{b.get('tool') or ''}({args})", "reason": b.get("reason"),
            "tool": b.get("tool")}
