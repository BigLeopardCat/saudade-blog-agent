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

**narrator 资产（P6 前半，20261005）**：系统提示从生产的 narrator 资产里接了两份
**逐字共用**的（见 `_system_prompt`）——`NARRATOR_DISCIPLINE`（纪律 1–23，含"读不到 ≠ 空"
与"不许派主人去登录"那两条）与 `STICKER_GUIDE`（8 个贴纸名的唯一名字表）。接之前
`own_*`（模型如实说读不到、却又多派一句"你先去登录"）与 `sticker_*` 两族整族慢性红。
纪律是 narrator（零工具节点）的口径，所以前面加了一段立场改写（`_DISCIPLINE_STANCE`，
同 `audience_block` 的手法：只换指称、实质一条不放宽）。

**这一版明确不做的**（都是 `docs` 里 P4–P6 的活，别当成已经做完了）：
  · 闸门谓词（P4）⇒ `gate.fallback_text`/`gate_replan` 恒不发，`forbid_fallback`
    在 react 臂上**不可证伪**（这正是 P0 要给它的合成正控）;
  · 跨轮执行记忆 + task 行（P5）⇒ 不发 `task_frame`、不读不写台账族；
  · narrator 的**独立节点**与动作事实块（P6 后半）⇒ 收尾仍由**同一个模型**写，
    `factblock.render_fact_block` 那一格还没接（叙述权没有收归系统）。

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

from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage, ToolMessage
from langchain_core.tools import StructuredTool

from agent.context import (_doc_anchors, _frame_texts, _last_assistant_utterance,
                           _last_user_msg, _page_ctx, _recent_tail, _short_reply_hint)
from agent.decisions import _article_fast_path, _intent_hints
from agent.graph import (NARRATOR_DISCIPLINE, _confirm_popup, _pending_ledger_frame,
                         _principal_of, _render_planner_prompt)
from agent.native_plan import build_tool_schema
from agent.prompts import BLOG_ASSISTANT_PROMPT, STICKER_GUIDE, audience_block
from agent.react_line import RunLedger, SkillExecutor, build_agent
from agent.refs import ref_hints
from agent.skills import instantiate_plan
from config.settings import settings
from models.llm import get_llm
from tools import get_all_tools

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

# 首轮决策的轮次说明。生产那一句会写"第 N/4 轮"（planner 每轮重渲染提示词），
# 而本适配器的系统提示**整轮只渲染一次**（提示词是 `create_agent` 的构造参数）——
# 所以这里只能给一句**不随轮次变**的说明。这是 P6 之前最明显的口径差：planner 每轮
# 都知道自己还剩几轮，本线的模型不知道（只有 `ConvergenceMiddleware` 在背后数）。
_ROUND_INFO = (
    "当前决策：**自由循环**（不是一次性的计划）。你可以在多轮里连续调用技能——"
    "每一轮挑一件最要紧的事、看回执、再决定下一步；**信息已经够了就直接给出给主人看的"
    "最终答复**（那条答复不要再调用任何工具）。")


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
        # 生产在 planner 的首轮、**任何 LLM 之前**判它（`graph.py:4601` 的
        # `rounds == 0 and not has_frames`）：主人当前页是文章详情页且这句引用了
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
        try:
            # 内层只看得到本轮那一句（理由见模块头注的最后一条口径偏差）——但那一句
            # **带着它的多模态部件**（见 `_inner_message`：按文本重建会把图片丢掉）。
            for step in agent.stream({"messages": [_inner_message(messages, user_msg)]},
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
                            # 这一轮点名的技能（`plan_obj`）：`_plan_skill` 只读它，
                            # 而令牌里签的技能名就是它——取不到 ⇒ 空串 ⇒ `sign` 拒绝
                            # 签发 ⇒ 卡弹不出来。所以按"最近一次有效计划"记着。
                            plan = _plan_of(m.tool_calls, role) if m.tool_calls else {}
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
                            yield from self._on_wrap_up(m)
        except Exception as e:  # noqa: BLE001 —— 内层炸了也要给主人一句诚实的话
            # 不把异常放走：golden 一条用例炸掉会让整轮跑丢一条读数，而"这臂办不成事"
            # 与"这臂崩了"在报告里必须**长得不一样**。所以照样产一条收尾正文（确定性、
            # 不含任何执行声称），并留 WARNING（原样吞掉才是真的查不出来）。
            logger.warning("[react_arm] 内层循环异常，按诚实收尾处理：%s", e,
                           exc_info=True)
            yield from self._crash_tail(ledger, e)

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
    """决策用 LLM：**与生产 planner 同一组解码参数**。

    `GOLDEN_ARM` 的两臂必须同解码，否则比的是解码参数而不是循环结构——20261004 那次
    "17× 成本 / 33% 撞上限"的假读数就是这么来的（离线 A/B 头注记着：裸 `get_llm()` 是
    thinking=True + temp 0.7 + 8192 预算）。生产 planner 的三个值走 settings
    （`.env` 里 `PLANNER_NATIVE_THINKING=false`），这里照读同一组，不另写常数。
    """
    return get_llm(temperature=0.2,
                   max_tokens=settings.planner_native_max_tokens,
                   timeout=settings.planner_native_timeout,
                   enable_thinking=settings.planner_native_thinking)


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
    """系统提示 = 判断器提示词（生产的唯一渲染入口）+ 人设 + **叙述纪律** + 循环说明。

    **为什么拼这几块**：本线一个模型既做决策又写正文（没有独立 narrator 节点）。
    · 第一块就是 planner 逐字那份（`_render_planner_prompt`，"唯一入口"）——技能表、
      规则、输出契约全在里面，**照着抄第二份就是抄一个漂移源**；
    · 第二块是 `BLOG_ASSISTANT_PROMPT`（人设 + 叙述边界 + 诚实底线，narrator 用的同一份
      资产）。没有它，收尾正文是"规划器口吻"的、且没有任何反幻觉纪律；
    · 第三块是 `NARRATOR_DISCIPLINE`（20261005 起，**与生产 narrator 逐字同一份**，
      `graph._EXECUTOR_PROMPT` 的纪律段）。此前一条都没接，代价实测可数：`own_*` 族
      整族慢性红——模型如实说了"读不到"，却**又多派了一句"你先去登录"**，而纪律 20
      明令禁止（生产的账不许算在主人头上）。同族还有"读不到 ≠ 空"那几条；
    · 第四块是 `STICKER_GUIDE`（情绪贴纸的**唯一**名字表）。不接它，`:害羞:` 这些
      记号结构上产不出来（`sticker_*` 用例整族恒红），而语料只认这 8 个名字；
    · 第五块是循环机制（怎么终止），只有三五句。

    **纪律块要带立场改写**（`_DISCIPLINE_STANCE`）：那份纪律是 narrator（零工具节点）
    的立场——第 1 条写着"你没有任何可以直接调用的工具"。本循环正好相反，所以紧挨着它
    前面放一段"哪几句按本循环读、其余一个字不放宽"。**不改写就是自相矛盾的提示词**，
    而矛盾提示词的失效方式是不定向的。
    """
    try:
        ledger_frame, _meta = _pending_ledger_frame(
            user_msg, _last_assistant_utterance(messages), principal, config)
    except Exception:  # noqa: BLE001 —— 台账读不到不该炸掉整轮（它只是背景事实）
        logger.warning("[react_arm] 待办台账读取失败，按「没有台账」处理", exc_info=True)
        ledger_frame = ""
    planner = _render_planner_prompt(
        role=role, page_ctx=_page_ctx(messages, role), round_info=_ROUND_INFO,
        user_msg=user_msg,
        # `executed` 传空表：本线没有 planner 那样的"本轮执行过什么"状态（工具结果
        # 就在消息流里，模型自己看得见），而提示词这一格只说"扫到的动作完成没完成"
        # ——给空的后果是动作被标成"**未完成**"，即"别忘了做"，方向是安全的那一边。
        intent_hints=_intent_hints([], user_msg),
        doc_anchors=_doc_anchors(messages),
        recent_context=_recent_tail(messages),
        short_reply_hint=_short_reply_hint(messages),
        # 工具帧与引用提示都走各自 helper 的**空输入**取值（`（本轮尚无工具执行）` /
        # `无从引用`）——不手写这两句话：helper 的措辞改了这个适配器要跟着改，是同一份
        # 漂移源。工具结果本身在消息流里（ToolMessage），模型看得见。
        #
        # 例外是**当前文章读取快道**（`fast_frames`，20261005）：那一次读取发生在
        # 内层循环**开始之前**，消息流里不会有它的 ToolMessage，所以它的结果必须由
        # 这一格交给模型——渲染仍走 `_frame_texts`（`get_article_detail` 是全文帧，
        # 那里有放宽的截断预算），不手写摘要。
        tool_results=_frame_texts(list(fast_frames or [])), ref_hints=ref_hints([]),
        pending_ledger=ledger_frame or "（本轮没有去读待办台账）",
        reflector_feedback="（本决策轮无复盘建议）",
        correction="（本决策轮无纠偏提示）")
    return "\n\n".join([planner, BLOG_ASSISTANT_PROMPT or "", audience_block(role),
                        _DISCIPLINE_STANCE, NARRATOR_DISCIPLINE, STICKER_GUIDE,
                        _CLOSING_RULES])


# 叙述纪律的**立场改写**（20261005）。同 `audience_block` 的手法：纪律文本只有一份，
# 变的只是"对谁、在哪个循环里读"——这里改的是**循环立场**那一维。
#
# 为什么必须有它：`NARRATOR_DISCIPLINE` 开篇就是"你是回复者，不是执行者""你没有任何
# 可以直接调用的工具。站内查询、跳转…都由系统在下面的执行计划中完成"。本线的模型
# **既是决策者也是回复者**，工具就在它手里——原样照读会得到一份自相矛盾的提示词
# （前面给的技能表说"调它"，紧接着说"你没有工具"）。
#
# **为什么是"逐条点名作废"而不是一句"你有工具"**（20261005 实测）：头一版只写了
# "你有工具、自己调用"，结果**工具调用整体掉了三格**——`guestboard_talk_double_source`
# /`after_offer_then_look_up` 变成"一个工具都没调"，`own_favorite_add_not_logged_in`
# 变成"没调 add_favorite"。对得上号的正是那几条**前提为假**的纪律：纪律 3 前半
# （"本轮尚无工具执行 ⇒ 如实说无法确认，或建议稍后再问"）在本循环里是"还没开始查"，
# 却被读成"答不了"；纪律 18（"要动站内数据的操作由系统自己走确认流程、确认框一个字
# 别提"）让模型**根本不碰写工具**。泛泛的一句"你有工具"压不住紧跟着的 23 条正文，
# 所以这里把作废的**条号与首句逐字点名**（模型对"第 3 条前半作废"这种指认比对话语
# 敏感得多），并**只作废前提为假的那半句**——同一号纪律里前提仍成立的那半（空结果
# ≠ 没执行、只按回执说结果）照旧生效。
#
# 边界写死：只改写**立场**，一条实质纪律都不放宽——尤其 20（读不到 ≠ 空 / 不许派主人
# 去登录）与"只按回执说结果"。
_DISCIPLINE_STANCE = """\
叙述纪律的读法（本循环的立场改写——纪律**原文**在后面，一字未改）：
下面那份纪律是生产 narrator 节点的（那个节点零工具、只负责把系统的执行记录说成人话）。
本循环里你**既是决策者也是回复者**，工具就在你手上。因此先按下面五条**作废/换指称**，
**其余各条（含同号纪律的后半句）一个字都不放宽**：

· **纪律 1 作废**（「你没有任何可以直接调用的工具…由系统在下面的执行计划中完成」）：
  你**有**工具，就是本文档前面技能表里那些，要做事就**自己调用**；纪律里说的
  「执行计划」在本循环里 = 你的技能表。
· **纪律 3 的前半句作废**（「工具执行记录为本轮尚无工具执行时…站内问题如实说明无法
  确认，或建议用户稍后再问」）：本循环里"还没有工具记录"只说明你**还没开始查**——
  想回答就先调工具，**不许**用"无法确认"收尾。**后半句（空结果 ≠ 没执行）照旧生效**。
· **纪律 9 作废**（「回复遵循计划 REPLY 行的契约组织」）：本循环没有计划行，信息够了
  就直接写最终答复。
· **纪律 18 的前半句作废**（「要动站内数据的操作由系统自己走确认流程」）：写操作
  **你要自己调工具**——同意闸会在你真动手之前拦下并弹卡（那一轮轮不到你说话，与本条
  同理）。**后半句照旧生效**：只按回执说结果，绝不说"已经发起/已经办好了"。
· **纪律 23 作废**（它按「本轮已由系统印出的事实」那一格分岔，本循环没有那一格）：
  写族的结果**归你说**，按它 ② 那半执行——照回执原话说，不作完成式陈述。
· 纪律里凡说「工具执行记录 / 本轮执行回执」→ 你自己这一轮调用工具后拿到的真实返回
  （就在消息流里）。

其余全部照旧生效：不许编造、**读不到 ≠ 空**、只按回执说结果、不许把还没动手讲成已经
办好、不许给主人派「你先去登录」这类活……**这些一条都不放宽**。"""


# 收尾说明（模块私有，三五句）：把"这个循环里你怎么结束"讲清楚。**不写叙述纪律**
# ——那件事由上面的 `NARRATOR_DISCIPLINE` 负责（纪律前还有 `_DISCIPLINE_STANCE`），
# 这里只讲机制。
_CLOSING_RULES = """\
循环机制（你既是决策者也是回复者，同一条消息流里两者同体）：
1. 要做事就**直接调用技能工具**（上面的技能表就是你的全部本领）；系统会立刻执行并把
   真实结果作为一条工具消息还给你。
2. 信息已经够回答主人了 ⇒ **直接写出给主人看的最终答复**（那条消息里不要再带任何工具
   调用）——这就是本轮的结束。**不要**为了"走个流程"再调一次 chat 把话说出去。
3. 调用 `chat` 技能是**闲聊兜底**（把答复文本填进 reply），它不产生任何执行事实；能用
   一句正常的最终答复说清的事，就别绕它。
4. 同一件事不要重复做：工具回执里写着"未生效"就换参数或换工具，别原样重试。
5. 复核：说出口的每一件站内事实，都要能在上面的工具结果、页面上下文或执行回执里找到
   出处；找不到就如实说没查到。"""


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
