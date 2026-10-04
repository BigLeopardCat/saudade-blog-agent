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

**这一版明确不做的**（都是 `docs` 里 P3–P6 的活，别当成已经做完了）：
  · 确认卡 / 同意闸（P3）⇒ `pending_confirm`/`__CONFIRM__`/`__PENDING__` 一次都不发，
    语料里 `write` 类用例**不在**第一批可比子集里；
  · 闸门谓词（P4）⇒ `gate.fallback_text`/`gate_replan` 恒不发，`forbid_fallback`
    在 react 臂上**不可证伪**（这正是 P0 要给它的合成正控）;
  · 跨轮执行记忆 + task 行（P5）⇒ 不发 `task_frame`、不读不写台账族；
  · narrator 独立资产（P6）⇒ 收尾由**同一个模型**写，系统提示是"判断器提示词 + 人设/
    叙述纪律"拼的（见 `_system_prompt`）。

**已知口径偏差**（读第一批读数时必须带着看）：
  · `parallel_tool_calls` 没有钉住——`create_agent` 自己 bind_tools，生产 planner 是
    `parallel_tool_calls=False`（见 `native_plan.bind_native` 的注）。本适配器**容忍**
    一轮多条调用（合并成一份 `plan_obj`、逐条发 `execute`），所以偏差表现为"步子更碎"，
    不会丢调用；
  · 内层只看得到 `[HumanMessage(本轮用户消息)]`（与被测的 graph planner 信息面同源：
    page_ctx / recent_tail / doc_anchors 都在系统提示里）。**不能**把整条生产消息流塞进去
    ——`ConvergenceMiddleware` 数的是 state 里 `AIMessage` 的条数，历史轮次的 AI 消息会
    把轮数预算吃光（多轮用例会第一轮就"预算用满"收尾，读数看着像"这臂什么都办不成"）。
"""
from __future__ import annotations

import json
import logging
from typing import Any, Iterator

from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage, ToolMessage
from langchain_core.tools import StructuredTool

from agent.context import (_doc_anchors, _frame_texts, _last_assistant_utterance,
                           _last_user_msg, _page_ctx, _recent_tail, _short_reply_hint)
from agent.decisions import _intent_hints
from agent.graph import _pending_ledger_frame, _principal_of, _render_planner_prompt
from agent.native_plan import build_tool_schema
from agent.prompts import BLOG_ASSISTANT_PROMPT, audience_block
from agent.react_line import RunLedger, SkillExecutor, build_agent
from agent.refs import ref_hints
from agent.skills import instantiate_plan
from config.settings import settings
from models.llm import get_llm
from tools import get_all_tools

logger = logging.getLogger(__name__)

ARM_NAME = "react"

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
        executor = SkillExecutor(_real_tools(), ledger, role=role, principal=principal)
        agent, ledger = build_agent(
            _llm(), _skill_menu(role),
            system_prompt=_system_prompt(messages, role, principal, config, user_msg),
            budget=BUDGET, executor=executor, ledger=ledger, name="react_arm")

        sent_calls = 0
        try:
            # 内层只看得到本轮那一句（理由见模块头注的最后一条口径偏差）。
            for step in agent.stream({"messages": [HumanMessage(content=user_msg)]},
                                     {"recursion_limit": REC_LIMIT},
                                     stream_mode="updates"):
                for node, upd in (step or {}).items():
                    for m in list((upd or {}).get("messages") or []):
                        if node == "model" and isinstance(m, AIMessage):
                            yield from self._on_model(m, role)
                        elif node == "tools":
                            sent_calls = yield from self._on_tools(
                                executor, ledger, sent_calls)
        except Exception as e:  # noqa: BLE001 —— 内层炸了也要给主人一句诚实的话
            # 不把异常放走：golden 一条用例炸掉会让整轮跑丢一条读数，而"这臂办不成事"
            # 与"这臂崩了"在报告里必须**长得不一样**。所以照样产一条收尾正文（确定性、
            # 不含任何执行声称），并留 WARNING（原样吞掉才是真的查不出来）。
            logger.warning("[react_arm] 内层循环异常，按诚实收尾处理：%s", e,
                           exc_info=True)
            yield from self._crash_tail(ledger, e)

    # ── 帧合成 ───────────────────────────────────────────────────────
    def _on_model(self, m: AIMessage, role: str | None) -> Iterator[tuple[str, Any]]:
        """内层的一次模型轮：要么是**决策**（带 tool_calls），要么是**最终答复**。"""
        if m.tool_calls:
            plan = _plan_of(m.tool_calls, role)
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

    def _on_tools(self, executor: SkillExecutor, ledger: RunLedger,
                  sent_calls: int) -> Iterator[tuple[str, Any]]:
        """内层的一次工具轮：真工具调用**已经发生**（executor 在包装层里跑完了）。

        顺序与生产同源：工具帧（`messages`）先到，`execute` 的 update 后到——
        `graph.execute_node` 也是先产 ToolMessage 再交回 update。
        """
        new = executor.calls[sent_calls:]
        for i, c in enumerate(new, start=sent_calls):
            yield ("messages", (ToolMessage(content=str(c.get("result") or ""),
                                            name=str(c.get("tool") or ""),
                                            tool_call_id=f"react_{i}"), {}))
        # 回执是**累计语义**（生产 execute_node 每次交的都是请求内全部 PASS 行，
        # producer 靠 `receipt_sent` 下标算增量）⇒ 这里也必须给全量。
        yield ("updates", {"execute": {
            "receipts": list(ledger.receipts),
            "blocked": [_blocked_row(b) for b in ledger.blocked]}})
        return len(executor.calls)

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


def _real_tools() -> dict[str, Any]:
    """真工具名 → 可调用体。**必须是纯函数签名（`**kwargs`）**：

    `SkillExecutor` 是 `fn(**args)` 调的，而 `BaseTool.__call__` 的第一个位置参数是
    `tool_input` ⇒ 直接塞 `StructuredTool` 会把它当成"参数名恰好叫 path 的 tool_input"，
    症状是**被包装层兜成错误帧**（不炸图），看起来像"每个工具都 BLOCK error_frame"
    ——判据在开火，其实是适配器写错了。这里统一走 `.invoke(单个 dict)`，与生产
    `graph.execute_node` 的调用形态一致。
    """
    return {t.name: (lambda _t=t, **kw: _t.invoke(kw)) for t in get_all_tools()}


def _system_prompt(messages: list, role: str | None, principal: Any,
                   config: dict | None, user_msg: str) -> str:
    """系统提示 = 判断器提示词（生产的唯一渲染入口）+ 人设/叙述纪律 + 循环说明。

    **为什么拼这三块**：本线一个模型既做决策又写正文（P6 之前没有独立的 narrator 资产）。
    · 第一块就是 planner 逐字那份（`_render_planner_prompt`，"唯一入口"）——技能表、
      规则、输出契约全在里面，**照着抄第二份就是抄一个漂移源**；
    · 第二块是 `BLOG_ASSISTANT_PROMPT`（人设 + 叙述边界 + 诚实底线，narrator 用的同一份
      资产）。没有它，收尾正文是"规划器口吻"的、且没有任何反幻觉纪律；
    · 第三块是循环机制（怎么终止）。**这三块都不是新造的资产**，第三块只有三五句。
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
        tool_results=_frame_texts([]), ref_hints=ref_hints([]),
        pending_ledger=ledger_frame or "（本轮没有去读待办台账）",
        reflector_feedback="（本决策轮无复盘建议）",
        correction="（本决策轮无纠偏提示）")
    return "\n\n".join([planner, BLOG_ASSISTANT_PROMPT or "", audience_block(role),
                        _CLOSING_RULES])


# 收尾说明（模块私有，三五句）：把"这个循环里你怎么结束"讲清楚。**不写叙述纪律**
# ——那件事由上面的 BLOG_ASSISTANT_PROMPT 负责，这里只讲机制。
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
