# -*- coding: utf-8 -*-
"""golden 的第二条臂：把 ReAct 试验线接进评测框架（`GOLDEN_ARM=react`）。

**它是什么**：`eval/golden_arm.py::build_agent()` 在 `GOLDEN_ARM=react` 时 import 本模块的
`build()`，把返回的对象装在 `server._agent` 上，生产那半
（`server._run_agent_stream_to_queue`）当图来消费。**生产代码一个字节都不改**——所以本模块
的全部工作可以一句话说完：

    用另一套循环（`agent.react_line` 的收敛判据 + 回执台账），产出**一模一样的那几类帧**。

**缝为什么必须长这样**：生产者按名字消费 `updates` 里恰好四个节点
（`planner` / `execute` / `model` / `gate`），并只把 `langgraph_node == "model"` 的
`messages` 分片当用户可见正文。

## 20261016 起：它是一张**真的 LangGraph 图**（不再有适配器）

此前本模块是一个 900 行的 `stream()` 生成器：手搓 `("updates", {...})` 二元组、给分片
**冒充** `meta["langgraph_node"]="model"`、给 `gate_node` / `_confirm_popup` 喂**合成出来的**
state（`frames.round_state` / `popup_state`）。现在这四样东西在结构上不可能存在：

  · **节点名恒为** `planner` / `execute` / `model` / `gate`（= 生产者从 `updates` 里现取的
    四个键），名字不再是"声称"，是 `add_node` 的结果；
  · **state 是生产那一份**（`graph.AgentState`），`gate_node` 与 `_confirm_popup` 拿到的是
    **真 state** ⇒ 三份塑形器（`round_state` / `popup_state` / `gate_state`）全死，那张
    "手抄的键集表"再没有存在的理由（批 1 实测：两张手抄表合起来漏了 8 格）；
  · **分片由 LLM 流给**，不是臂手搓的 ⇒ 冒充消失（`langgraph_node` 由 langgraph 盖）；
  · **`create_agent` / `ConvergenceMiddleware` / `build_agent` / `wrap_tools_with_receipts`
    整体退场**（收敛判据搬成纯函数 `react_line.convergence_verdict`，由 `planner` 节点调）。

拓扑（`build_graph()`）：

    START → planner ─┬─(有技能调用)──→ execute ─┬─(弹卡/零改动)──→ END
                     │                          └─(还有得做)─────→ planner
                     ├─(零工具 = 收尾)─→ model ──→ gate ─┬─(gate_replan，只此一次)→ planner
                     └─(预算/无进展)───→ model            └────────────────────→ END

**入口是 `START → planner`（没有入口条件边）**：快道判据住在 `planner` 里，因为
`add_conditional_edges(START, …)` **写不了 state**——绕过 `planner` 就没人写
`plan_obj`/`plan_rounds`，生产者那一格 `updates["planner"]` 会缺席（`🧭 规划中…` 不发），
而且路由顺序会把 `updates` 帧的次序打乱。

三条**铁律**（每一条都有对应的假读数，见 `eval/arm_capability.py` 要防的那一族）：

  ① **只有最终答复上 `messages` 通道的正文**。`run_golden.run_one` 把每个
     `AIMessageChunk` 拼进 `text`。本图里 `planner` 节点也在调 LLM（也流式），它的分片
     带的是 `langgraph_node="planner"` ⇒ **生产者按节点名滤掉**（两条消费路径都是），
     golden 读的是生产者的队列、不是原始流 ⇒ 中间推理不会污染任何文本断言。
     ⚠️ 这条靠的是"生产者过滤"这唯一一道闸：直接消费 `graph.stream(...)` 的读者
     （`tests/test_react_arm.py` 的 `_chunks`）**必须自己按节点名滤**，判据与生产者同款。
  ② **`ToolMessage` 必须带真工具名**。本线的模型菜单是**技能级**的，而
     `require_tool_calls`/`require_zero_exec` 认的是 `_TOOL_REGISTRY` 里的真名 ⇒ 技能名
     会让那两族**静默失真**（`require_zero_exec` 更坏：写工具永远不在 `tool_calls` 里
     ⇒ 恒绿）。所以 `execute` 节点逐条产 ToolMessage，`name` 取 `executor.calls` 里的真名。
  ③ **回执按生产形状**（`tool/args/result/ts` + PASS 行的 `cmd`）——`__EXEC__`/`__CMD__`
     两条帧、以及那 131 条 `*/cmd_*` 断言的全部输入。

**写操作确认卡（P3，20261005）**：写 spec 卡在同意闸上时——`WorkflowExecutor` 判
「需要同意 ∧ 这一轮没获同意」⇒ **不执行**、记进 `ledger.pending`（判据直调生产
`graph._grant_covers`，见 `_consent_missing`），`execute` 节点收尾时调 **`graph._confirm_popup`
本身**（不抄第二份排序/快照/滤空逻辑）拿到 `pending_confirm` / `pending_action` /
`confirm_text`（或"状态已达成"那支的 `noop_text`/`noop_note`），原样交回 producer ⇒
`__CONFIRM__`/`__PENDING__` 两条控制帧由生产那半发。弹卡轮**到此为止**（`route_after_execute`
→ END，与生产同一条边），正文由 producer 用 `confirm_text` 发**一次**——臂这边一个字节的
正文都不补，补了就是两遍。

**narrator 独立节点（P6，20261012）**：收尾不由决策模型自己写——`planner` 给出"没有工具
调用了"那条消息之后，正文改由 `model` 节点里的**独立的零工具调用**产出（`_narrate` /
`_narrator_call`），提示词是生产 narrator 那一份（`graph._EXECUTOR_PROMPT`，含
`NARRATOR_DISCIPLINE` 与 `STICKER_GUIDE` 两块共享资产），**决策模型那条草稿不进提示词**
（生产 narrator 也看不到任何草稿），只在 narrator 两次都拿不出正文时兜底。**闸门判的是
narrator 写出的那一段**——同一条消息流里"决策模型自己写收尾"时，它手上就有工具，"我调用了
X"既可能是回忆也可能是谎言；生产那个被判的模型是**零工具节点**，那句话从它嘴里出来必然是
声称。判的对象不同种，两臂就不可比。**P6 是行为改动** ⇒ 本线自此与 `20261005_203521_332`
那份读数**不可比**，旧读数在文档里一律标注为「P6 之前」。

**闸门谓词（P4，20261011）**：这条臂此前**结构上不会发 fallback** ⇒
`arm_capability --arm react` 判 `comparable = False`（138 条落在 `forbid_fallback`，全白发）。
接法是 `gate` 节点**直调 `graph.gate_node`**（`_prod_gate_node`），判据
（`_REPLAN_ISSUES` 约 20 族 / `_claim_issue` / `_fallback_result` / `_replan_result`）
**一条都没抄**：抄第二份就是抄一条漂移源，而它恰好是本仓最常见的缺陷。
`gate → planner` 那条边（打回重规划**只此一次**）由图自己走，`RemoveMessage` 因此
**真生效**——被否定的那条 AI 消息确实在 state 里（旧适配器得手动把它丢掉，因为内层
每次 `invoke` 都从空 state 起）。

**已知口径偏差**（读读数时必须带着看，`docs/adr/adr-0009-*` 有完整清单）：
  · **真流式是刻意引入的**：narrator 的分片由 LLM 流给（生产今天就是这个形状，
    `settings.llm_streaming=True`）。代价：分片在闸门**之前**就出去了（生产同形，
    `__RESET__` 负责撤屏）；`_narrator_call` 的"回了 tool_calls 再纠正一次"那一支会让
    主人看到两段（**生产有同一个洞**——同一段代码、同一个流式开关，本批不发明第三套处置）。
  · **`gate` 节点的异常吞掉按 pass 处理**（留 WARNING）：闸门是加装的一层，它自己炸了
    不该把主人这一轮的答复整条吃掉——生产那边会被 langgraph 变成整轮错误。**这条口径差
    是明写的，不假装同源。**
  · **`planner` 的提示词每请求只渲染一次**（`ctx["system_prompt"]` 缓存），生产是每轮
    重渲染（`rounds`/`frames_txt`/`blocked_rows`/`ref_hints` 都会变）。改它会让 A/B 读出的
    差异分不清是结构还是提示词，所以本批不动。
  · **`planner` 读的是真消息窗口**（本轮用户消息 + 工具帧 + 纠偏提示），生产 planner 读的是
    `_render_planner_prompt` 渲染出来的**一个字符串**（工具帧走 `tool_results` 那一格）。
    两边的信息面同源，形态不同。
  · **快道那一圈不吃决策预算**（`advance=False`）：生产那条快道是
    `_plan_update(plan_obj, rounds)`（默认 `advance=True`）。本线按旧臂的口径（快道在循环
    之外、不占轮）——改它会让读数里混进一条与结构无关的预算差。
  · **意图纠偏不吃决策预算**（纠偏轮 `return {}`）：同上，纠偏是"同一轮的第二次决策"。
  · **`_narrator_call` 在原文上就地改 `resp.content`**（贴纸记号修复），与生产
    `model_node` 同款——**不是**另建一条 `AIMessage`：重建会换掉 id，而那条 id 已经被
    `messages` 通道认领过（`on_llm_end` 的 dedupe 靠它），重建 = 同一段正文发两遍。
  · `parallel_tool_calls` 没有钉住——生产 planner 是 `parallel_tool_calls=False`
    （见 `planner_llm.bind_planner_tools` 的注）。本线的 `bind_tools` **不传**它，容忍一轮多条
    调用（合并成一份 `plan_obj`、逐条发 `execute`），所以偏差表现为"步子更碎"，不会丢调用。
  · 内层只看得到 `[HumanMessage(本轮用户消息)]`（与被测的 graph planner 信息面同源：
    page_ctx / recent_tail / doc_anchors 都在系统提示里）。文本按 `_last_user_msg` 取末
    500 字，**非文本部件（图片）原样带着**（见 `_inner_message`）。**不能**把整条生产
    消息流当"内层输入"塞给一个自由循环——本图不是那样用的：轮数预算数的是 `plan_rounds`
    （不是 `AIMessage` 条数），所以 DB 历史里那些 AI 消息不会吃掉预算。

**这一版仍然不做的**（别当成已经做完了）：
  · **trace 的节点事件这一族**（20261012 全量双臂实测抓出）：本线每轮**只**留下
    `producer.*` 与 `gate.*` 两类事件（gate 那几格是直调 `graph.gate_node` 顺带记的），
    生产那半每轮的 `planner.llm_done` / `native_decision` / `decision` /
    `execute.call` / `execute.check` **一个都没有** ⇒ L3 跨源对账
    （`eval/trace_reconcile.py`）对本线近乎无料可读。
    **判据后果只兑现了一处、且已修**：`planner.ledger_frame` 缺席让
    `require_ledger_frame` / `require_ledger_rows` / `require_card_targets_from_ledger`
    三条断言在本线上**恒假**（全量里 `admin_board_audit_reviewed_refusal` 因此报红，
    而两臂那一轮的行为**逐字相同**——回复正文一字不差、卡载荷的 workflow/tool/args 全同）
    ⇒ `_system_prompt` 现在照 `graph.py` 的 planner 那一处记同款事件，判据
    `tests/test_react_arm.py` ⑬（含负控）。这与 `forbid_fallback` 是同一个病的两个方向：
    那边"做不到 ⇒ 纯否定断言恒真"（假绿），这边"不产证据 ⇒ 要求型断言恒假"（假红）。
    **其余节点事件的补记仍未做**——它今天一个读数都不改变（没有断言读它们），补它是为
    将来 L3 与本臂真上线，不是为这份读数。**拆成真节点之后补它变得便宜**（每个节点函数
    自己 `record(...)`），那是批 3。
    （20261019 资产对齐 G 把这一族补了三处**有消费方**的：`planner.task_declare` /
    `planner.task_auto` 由 `_decide_register` 与 `_auto_task_frames` 自己记（生产同一份
    代码），`planner.native_fallback` 的 `unknown_tool` 那一档是本臂补的。剩下的
    `llm_done` / `native_decision` / `decision` / `execute.call` / `execute.check`
    仍然没有。）
  · **`task_frames` 20261019 起已发**（原记在这里的"不发"作废）：三个伪函数从调用里
    摘出来、`_decide_register` 与 `_auto_task_frames` 直调生产那一份，帧经
    `planner_node` 的薄壳并进 `task_frames`。见 `agent/react_parity.py` §G 与
    `tests/test_react_task_ledger.py`。
  · （20261012 划掉两条此前记在这里的"缺口"——**实测都不是缺口，所以一个字都没改**：
    · `__EXEC__` **已经在发**：`execute` 的 `receipts` 里就带着 PASS 行的 `cmd`，而
      `__EXEC__` 是 `server._run_agent_stream_to_queue` 从 updates 里读的；
    · 跨轮执行记忆（`recent_executions`）与 `open_tasks` **已经读到了**：
      `server._build_messages` 把它们写进那条 `[System: …]` 块，`_page_ctx` 把整块提
      出来，`_system_prompt` 已经把它给了 planner 提示词那一格（与生产 planner/model
      两条腿同一个通道）。**不另写第二个渲染器**——同一份事实两个渲染器就是漂移。）
  · **动作事实块这一格 20261005 起不再是缺口**：生产两臂都停止代印（
    `agent/factblock.py` 的 `BLOCK_FAMILIES` 空集）⇒ "叙述权收归系统"这件事整体歇业，
    本线"没接它"反而与 graph 臂口径一致了（此前它是两臂的一个真实差异）。
"""

import logging
import time
from collections.abc import Hashable
from typing import Any, cast

from langchain_core.messages import (AIMessage, AIMessageChunk, HumanMessage,
                                     SystemMessage, ToolMessage)
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import StructuredTool
from langgraph.graph import END, START, StateGraph

from agent.deterministic_plan import _wrap_up_plan
from agent.factblock import block_of, is_action_family, is_block_family

# 帧的形状全在 `agent/frame_contract.py`（20261016 起）——**本模块只负责"什么时候发"**，
# 而"发出去长什么样"现在由**节点名 + 节点返回值**结构性地决定（见模块头注）。
from agent.frame_contract import (NOOP_TEXT,
                          blocked_row as _blocked_row,
                          executed_specs,
                          plan_for_gate as _plan_for_gate)
from agent.graph import (AgentCancelled, AgentState, NARRATOR_DISCIPLINE, _NARRATOR_NO_TOOLCALL_NUDGE,
                         _confirm_popup,
                         _frame_id, _pending_ledger_frame,
                         _principal_of, _render_planner_prompt, _stopped,
                         gate_node as _prod_gate_node, graph_input, plan_state)
from agent.llm_usage import usage_fields
from agent.planner_llm import build_tool_schema
from agent.prompt_context import (_frame_texts, _has_frames,
                           _last_assistant_utterance, _last_user_msg, _msg_text,
                           _page_ctx, _prev_user_msg, with_tool_call_pairs)
from agent.prompts import BLOG_ASSISTANT_PROMPT, STICKER_GUIDE, audience_block
from agent.react_line import (RunLedger, WorkflowExecutor, convergence_verdict,
                              spec_signature, wrap_up_text)

# 借生产那几件的**唯一入口**（20261019）。本模块只接线，判据都在 `agent/graph.py` 里；
# 各段的接法与理由见 `agent/react_parity.py` 的模块头注。
from agent.react_parity import (auto_task_frames, confirm_round, denied, display_text,
                                drop_correction, fast_path, finalize_dedupe,
                                narrator_system_pair, narrator_wire, plan_has_tools,
                                planner_prompt_args, register, split_pseudo_calls,
                                strip_injected_args, task_decision, unaccounted,
                                unknown_workflows, write_guard)
from agent.stickers import repair_sticker_tokens
from agent.tool_specs import _tool_name
from agent.workflow_plan import instantiate_plan
from config.settings import settings
from models.llm import get_llm
from tools import get_all_tools
from utils.trace import record

logger = logging.getLogger(__name__)

ARM_NAME = "react"

# 一次运行一个实例的那几件（`RunLedger` / `WorkflowExecutor`）**不进图 state**：图是
# `build()` 时编一次、跨请求共用的单例，账进 state 才是对的、账进闭包就会串账。它们走
# `config["configurable"]`——与生产把 `stop_event` / `principal` / `open_tasks` 放那儿
# 同源（`server.py` 的 `_run_agent_stream_to_queue`）。
#
# ⚠️ langgraph **不深拷贝 config**（20261016 实测）：每个节点拿到的都是同一个
# `configurable` dict，所以这一格既是"每请求一份"的隔离点，也是节点之间唯一的共享内存。
_CTX_KEY = "react_ctx"


def _maybe_ctx(config: RunnableConfig | None) -> dict:
    """这一轮的运行上下文（路由函数用——它们可能在任何节点之前/之后被调）。"""
    return ((config or {}).get("configurable") or {}).get(_CTX_KEY) or {}


def _ctx(config: RunnableConfig | None) -> dict:
    ctx = _maybe_ctx(config)
    if not ctx:
        raise RuntimeError("[react_arm] config 里没有 react_ctx —— 本图只能在 "
                           "ReactGoldenArm.stream() 下跑（它负责建账并注入）")
    return ctx


def _guard(config: RunnableConfig | None) -> None:
    """节点级中断检查（与 `graph.py` 各节点开头那一句同款）。"""
    if _stopped(config):
        raise AgentCancelled()


def _inner_message(messages: list, user_msg: str) -> HumanMessage:
    """内层循环看到的那**一条**用户消息：文本仍取 `user_msg`（末 500 字的口径），
    **但把多模态部件（图片）原样带过去**。

    病（20261005 实测，`image_color_red`/`image_two_colors` 两条慢性红）：主人发的消息
    `content` 是 `[{"type":"text",…},{"type":"image_url",…}]`，而本线此前写的是
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
    # 标 `list[Any]`：多模态部件的形状是上游（前端 → Rust → 这里）给的，本层只做搬运，
    # 不假装知道它长什么样。不标的话 mypy 把这条推导成 `list[dict[str, str]]`，而
    # `HumanMessage.content` 的联合类型里那个 `list` 是不变的 ⇒ 拼起来判红。
    extra: list[Any] = [p for p in content
                        if isinstance(p, dict) and p.get("type") not in ("text", "input_text")]
    if not extra:
        return HumanMessage(content=user_msg)
    return HumanMessage(content=[{"type": "text", "text": user_msg}] + extra)


# 模型决策轮上限。生产 `MAX_PLAN_ROUNDS=4`；这里放宽到 6 是本线既有的取值
# （一轮里可能并发多条调用，步子比 planner 碎），离线 A/B 用的也是 6 ⇒ 与既有读数可比。
BUDGET = 6
# 图级保险丝（同离线 A/B 的 REC_LIMIT）：预算判据自己会收尾，这一条只是兜底。
REC_LIMIT = 24

# 首轮决策的轮次说明。生产那一句会写"第 N/4 轮"（planner 每轮重渲染提示词），
# 而本线的系统提示**整轮只渲染一次**（见模块头注的口径偏差）——所以这里只能给一句
# **不随轮次变**的说明。这是 P6 之前最明显的口径差：planner 每轮都知道自己还剩几轮，
# 本线的模型不知道（只有收敛判据在背后数）。
_ROUND_INFO = (
    "当前决策：**自由循环**（不是一次性的计划）。你可以在多轮里连续调用技能——"
    "每一轮挑一件最要紧的事、看回执、再决定下一步；**信息已经够了就直接给出给主人看的"
    "最终答复**（那条答复不要再调用任何工具）。")


# ══════════════════════════════════════════════════════════════════════════════
# 每请求上下文（`config["configurable"]["react_ctx"]`）
# ══════════════════════════════════════════════════════════════════════════════

def _chunk(text: str) -> AIMessageChunk:
    """确定性正文的**唯一出口**：`AIMessageChunk`。

    必须是分片类型：生产者把用户可见正文只认 `isinstance(chunk, AIMessageChunk)`
    （`server.py` 两条消费路径都是），而 `AIMessage` **不是它的子类**——返回一条
    `AIMessage` 会让这段正文只进 trace、不进主人屏幕（`updates` 那半照样读到它）。
    反过来 `AIMessageChunk` **是** `AIMessage` 的子类 ⇒ `updates` 那半的
    `isinstance(_m, AIMessage)` 也过。两边都认的只有这一个类型。
    """
    return AIMessageChunk(content=text)


def _expand(m: ToolMessage, ctx: dict) -> ToolMessage:
    """工具帧 → 模型看的那一份（**台账那一行回执接在正文末尾**）。

    `ctx["lines"]` 是 `tool_call_id → 回执行` 的映射，由 `execute` 节点在发帧时登记。
    回执行**只给模型看**（`react_line.RECEIPT_HEAD` 的纪律），所以它不能进
    `ToolMessage.additional_kwargs`（那会跟着帧走到生产者与 Rust 侧），也不该在
    `state["messages"]` 里——那里是**主人/闸门/narrator 看到的事实**，多一行"内部回执"
    就是往事实里掺一句给模型看的旁白。这里按需重建一份，原对象一个字节不动。
    """
    line = (ctx.get("lines") or {}).get(str(getattr(m, "tool_call_id", "") or ""))
    if not line:
        return m
    return ToolMessage(content=f"{m.content}\n{line}",
                       name=getattr(m, "name", "") or "",
                       tool_call_id=getattr(m, "tool_call_id", "") or "")


def _turn_tail(state: AgentState) -> list:
    """**本轮**的消息 = 最后一条 `HumanMessage` 之后的那些。

    `_window`（喂给模型的那份）与 `{tool_results}`（渲染进系统提示的那份）**共用同一
    刀**是刻意的：两处各切一次，"本轮"这个概念迟早会在一处漂成"整条消息表"。生产那侧
    的对应物是 `_turn(state)`（按 `turn_start` 切），切法同款、只是坐标来源不同。
    """
    msgs = list(state.get("messages") or [])
    idx = max((i for i, m in enumerate(msgs) if isinstance(m, HumanMessage)), default=-1)
    return msgs[idx + 1:]


def _window(state: AgentState, ctx: dict) -> list:
    """`planner` 这一轮看到的对话窗口。

    = 本轮用户消息（`_inner_message`，图片部件原样带）
    + **最后一条 `HumanMessage` 之后**的消息（工具帧 + 台账回执行 + 合成注记帧）。

    为什么要"最后一条 HumanMessage 之后"而不是"整条 `state["messages"]`"：那是
    **DB 历史 + 本轮**（`server._build_messages` 注入的 20 条 + 摘要）。历史已经由
    `_recent_tail` / `_doc_anchors` 渲染进系统提示（与生产 planner 同源），再把整条
    历史当消息喂进去是同一份信息发两遍、而且会让"本轮"这个概念消失。

    合成注记帧（`ctx["notes"]`）：零工具技能（`chat` 一类）的结果**不进
    `state["messages"]`**——那里是"系统确认的事实"，一条没执行任何工具的话塞进去，
    闸门的"有没有执行事实"判据（`_has_frames`）会当场误判。所以它只活在窗口里，
    由 `with_tool_call_pairs` 配一条**合法的**合成 assistant（`tool_calls[].name` 是
    技能名，而技能名就在菜单里）。
    """
    msgs = list(state.get("messages") or [])
    tail = [_expand(m, ctx) for m in _turn_tail(state)]
    notes = [ToolMessage(content=str(n.get("text") or ""),
                         name=str(n.get("workflow") or ""),
                         tool_call_id=f"note_{i}")
             for i, n in enumerate(ctx.get("notes") or [])]
    # ⚠️ 纠偏提示**不在窗口里**（20261019 资产对齐 E 改）：它走提示词的 `{correction}`
    # 槽，与生产 `_planner_setup` 的 `correction=` 逐字同路。此前这里额外挂一条
    # HumanMessage、而提示词那一格恒是缺省语（清空发生在 `_prompt_of` **之前**）——
    # 于是"纠偏进了提示词"这句话在 `react_parity.planner_prompt_args` 的表里是真的、
    # 在代码里是假的，两臂的提示词形状也就对不上。
    return ([_inner_message(msgs, ctx.get("user_msg") or "")]
            + _pair_frames(tail, ctx) + with_tool_call_pairs(notes))


def _pair_frames(tail: list, ctx: dict) -> list:
    """工具帧 → 模型看的那一份，**每条帧前面配一条带真参数的合成 assistant**。

    病（20261016 批 2 实测，rag 族整片红）：`with_tool_call_pairs` 合成的 assistant
    一律 `content=""`、`args={}` ⇒ 决策模型**看不到自己上一轮问的是什么**，只看到一串
    「`content_query`(空参数)」加几条结果正文。于是它认不出"这件事我刚做过"，反复重问，
    把轮次预算烧穿（`rag_git_branch` 实测：同样是 `rag_search`+`get_article_detail`
    两条回执，改前 1 轮收工、改后烧满 6 轮）。**旧臂没有这个病**：内层 `create_agent`
    累积的是模型自己那条真 `AIMessage`（带参数）。本函数把那一样补回来。

    分组：`origin` 指向**同一次调用**的连续帧合成一条 assistant（一次技能调用展开成几条
    帧是 `instantiate_plan` 的事，模型说的是一条）；没有 `origin` 的（系统快道之前发的、
    异常路径的）原样交给 `with_tool_call_pairs`——**不猜**。

    **只补参数、不补正文**：模型自己那条叙述（`content`）不是事实、也不进闸门，
    补它只是多烧 token；这里要的是"我问过什么"这一个可核对的坐标。
    """
    origin = ctx.get("origin") or {}
    calls = ctx.get("calls") or []
    out: list = []
    run: list = []
    key: Any = None
    for m in tail:
        k = origin.get(str(getattr(m, "tool_call_id", "") or ""))
        if run and k != key:
            out.extend(_pair_run(run, key, calls))
            run = []
        key = k
        run.append(m)
    if run:
        out.extend(_pair_run(run, key, calls))
    return out


def _pair_run(run: list, key: Any, calls: list) -> list:
    """一段（同一出处）工具帧 → `[合成 assistant, *帧]`；出处不明就退回老形状。"""
    if not isinstance(key, int) or not 0 <= key < len(calls):
        return with_tool_call_pairs(run)
    c = calls[key]
    return [AIMessage(content="", tool_calls=[
        {"name": str(c.get("name") or ""), "args": dict(c.get("args") or {}),
         "id": str(getattr(m, "tool_call_id", "") or ""), "type": "tool_call"}
        for m in run]), *run]


def _prompt_of(state: AgentState, ctx: dict) -> str:
    """系统提示——**每一轮重渲染**（20261019，资产对齐 B）。

    此前是"每请求渲染一次、缓存在 `ctx` 里"。那一版有一条**写在模块头注里的口径偏差**
    兜着它，代价是三格注入值恒为初值：`intent_hints` 读 `state["executed"]`、
    `tool_results` 读本轮工具帧、`ref_hints` 读 `state["tool_data"]`——三格都逐轮在变，
    缓存住等于第二轮的提示词在说"这一轮还没执行过任何工具、清单里没有未完成项"。
    而这三句正是规则 5（还有未完成项就别收尾）与规则 3b（照着 ref_hints 写引用）的
    **唯一输入**：提示词自己把前提擦掉，模型照做就是错的。

    重渲染的代价是每轮多拼一次字符串（几千字的 `format`），相对一次 LLM 调用可忽略。
    """
    return _system_prompt(state, ctx)


def _bound_llm(state: AgentState, ctx: dict) -> Any:
    """决策模型：**客户端每请求一份**（缓存在 `ctx`），**菜单每轮重绑**。

    为什么缓存的是客户端而不是"绑好的那一份"（20261019，资产对齐 B）：菜单这一格
    逐轮在变——`deny`（这一轮不许再选的技能）读的是上一轮的受阻项，而受阻项每轮都在
    长。缓存"绑好的那一份"等于让 `deny` **只可能为空**（首轮恒空）⇒ 刚失败过的技能
    永远留在菜单里，模型照着再点一次、同键二次受阻 ⇒ 收尾。

    重绑**不新建客户端**：新建出来的每次都是**另一个**客户端——连接池、`timeout`、
    上游的缓存命中全都跟着重置，而这是每一轮都发生的事。生产 planner 那侧正是这个
    形状（`_planner_setup` 里 `_base_llm = get_llm(...)` 每请求一次、`bind_planner_tools`
    每轮在它上面重绑）。
    """
    if ctx.get("llm") is None:
        ctx["llm"] = _llm()
    return ctx["llm"].bind_tools(_workflow_menu(ctx.get("role"), denied(state)))


def _norm_state(state: AgentState) -> AgentState:
    """`narrator` 与闸门看到的那份 state：**计划两态按 `plan_for_gate` 归一**。

    与 `frames.plan_for_gate` 逐字同源（那条链就是 `plan_state(plan_for_gate(plan_obj))`，
    批 2 之前它住在 `frames.round_state` 里，那个塑形器随适配器一起退场了）：
    `plan_obj` 为空（首轮就收尾、纯闲聊）时，生产那一轮的计划是 `chat`——不归一会让
    narrator 的 [执行计划] 段空着（生产从不空着），而 `gate_node` 拿空的 `workflow` 当分族
    入参会退化成宽查（方向安全，但读数与生产不是同一档）。
    """
    # `cast`：`plan_state` 的返回类型是 `dict`，拼出来的字面量 mypy 证不出是 `AgentState`
    # ——键集其实逐字就是它（`plan_state` 只写 `plan`/`plan_obj`，两个都是声明键）。
    return cast(AgentState, {**state,
                             **plan_state(_plan_for_gate(state.get("plan_obj") or {}))})


# ══════════════════════════════════════════════════════════════════════════════
# planner 节点：一轮决策（= 旧 `create_agent` 的一轮）
# ══════════════════════════════════════════════════════════════════════════════

def _plan_update(plan_obj: dict, rounds: int, task_frames: list | None = None, *,
                 advance: bool = True) -> dict:
    """决策出口的**唯一构造点**（形状与 `graph._plan_update` 逐字同款）。

    `advance=False`：这条出口**没花一次 LLM 决策**（快道是确定性的、纠偏是同一条消息的
    第二次决策）⇒ 不该吃轮数预算。理由与 `graph._plan_update` 那条注相同。

    `task_frames` **只有显式登记那一支传**（`_decide_register` 的 `[frame]`）——自动登记
    那几帧由 `planner_node` 的薄壳统一并进来，两条通道不必各写一遍"怎么把帧拼进 updates"
    （同 `graph._plan_update` 那条 `is not None` 的注：`[]` 与 `[frame]` 都是**显式**传的，
    「缺键」与「传空列表」不是一回事）。
    """
    upd = {**plan_state(_plan_for_gate(plan_obj)),
           "plan_rounds": rounds + 1 if advance else rounds, "done": False}
    if task_frames is not None:
        upd["task_frames"] = task_frames
    return upd


def _fast_plan(fast: dict) -> dict:
    """快道产出 → 执行节点读的那一份（**补上 `calls`**，它是清单的唯一来源）。

    `calls` 走 **`specs`** 那一格（清单已定稿）而不是 `params`（让 execute 重新
    实例化）：快道的产出**本身就是**计划。硬证据是 `effect_switch`——它返回**两条
    spec**（关旧 + 开新），而 `effect` 技能模板只有一条，重跑一遍 `instantiate_plan`
    会把它撕成一条。理由的全文见 `react_line.WorkflowExecutor.run_specs`。
    """
    obj = dict(fast or {})
    obj["calls"] = [{"workflow": str(obj.get("workflow") or "read_article"),
                     "params": dict(obj.get("params") or {}),
                     "specs": [str(t) for t in (obj.get("tools") or [])]}]
    return obj


def _correction_text(left: list) -> str:
    """纠偏提示（逐字搬自退场的 `ConvergenceMiddleware._intent_correction`）。"""
    pending = "、".join(f"{i['label']}（{i['key']}）" for i in left)
    return ("你这一轮**没有排任何工具**（等于宣布收尾），但主人那句话里还有这些"
            f"动作**一次都没有被执行过**：{pending}。你手里没有与它们相关的任何"
            "工具返回。本轮先把没做过的做掉（一轮一件），再谈收尾。")


def _stop_update(state: AgentState, ctx: dict, reason: str) -> dict:
    """收敛判据判停 → 确定性收尾计划（零 LLM）+ 收尾正文（`model` 节点照发）。

    `reason` 是**本线的判据名**（`budget` / `no_progress`），两处各取一份措辞：
      · `wrap_up_text`（给主人看的正文，说清"为什么停"）；
      · `_wrap_up_plan` 的 `reason`（进 narrator 提示词的 [执行计划] 段与 trace）。
    """
    word = (f"已达本线决策轮上限（{BUDGET}）" if reason == "budget"
            else "同一个操作连着重试没有进展")
    ctx["wrap_up"] = wrap_up_text(ctx["ledger"], reason)
    ctx["decided"] = "model"
    return _plan_update(_wrap_up_plan(_has_frames(_turn_tail(state)),
                                      reason=word),
                        int(state.get("plan_rounds") or 0), advance=False)


def planner_node(state: AgentState, config: RunnableConfig | None = None) -> dict:
    """一次决策。**本节点不往 `messages` 里写任何东西**（生产 planner 同款）。

    为什么这一条是硬要求：`with_tool_call_pairs` 会给每个"没人认领的工具帧串"补一条
    合成 assistant。planner 一旦也写消息（比如把自己那条带 tool_calls 的响应回写进
    state），那些帧就会被**认领**进 planner 的 assistant 里，而 planner 的 tool_calls
    与 execute 真发出去的 `tool_call_id` 未必对得上（`_frame_id` 是 execute 那一侧算的）
    ⇒ 出站序列非法（deepseek 一律 400，`_frame_id` 的 docstring 记着现场）。
    **决策由 `plan_obj["calls"]` 承载**，不靠消息表。

    **本函数是薄壳**（20261019 资产对齐 G，形状与 `graph.planner_node` 同款）：决策主体
    是 `_planner_decide`，壳只做一件事——把本轮**确定性登记**的任务帧并进返回。为什么
    要有这一层：`_planner_decide` 有十几条出口（收尾、纠偏、参数不齐、剔空、卡路径……），
    逐条补一个 `task_frames` 键等于把登记绑死在"作者记得的那几条"上（漏一条 = 那一族
    这一轮静默不登记，且没有任何测试会红）。所以只留**一个**注入点。
    `frames` 为空（绝大多数轮次）时返回的 dict 逐字不变。

    ⚠️ **sink 住在 `ctx` 里、不在这里现建**（与生产唯一的形状差）：生产的
    `_planner_decide` 内部有 `for _attempt in (0, 1)` 重决策循环，所以一次
    `planner_node` 调用里第一次尝试登记的帧能活到最终的返回。本臂的"重决策一次"是
    **再进一次本节点**（出口返回 `{}`、图把控制权交回 planner）⇒ sink 若现建就会在
    两次调用之间丢掉。放进 `ctx`（一次运行一个实例）之后，语义与生产那层壳逐字相同：
    登记帧活到**这一轮真正产出返回**的那一次，然后在返回时清空。
    """
    _guard(config)
    ctx = _ctx(config)
    sink: list = ctx.setdefault("task_sink", [])
    upd = _planner_decide(state, config, ctx, sink)
    frames = list(upd.get("task_frames") or []) + list(sink)
    sink.clear()
    if frames:
        upd["task_frames"] = frames
    return upd


def _planner_decide(state: AgentState, config: RunnableConfig | None,
                    ctx: dict, sink: list) -> dict:
    """决策主体（本臂的 `graph._planner_decide`）：一轮决策 → 节点的 state 更新。

    `sink` 是壳持有的"本轮确定性登记"载荷（见 `planner_node` 的 docstring）；
    `_planner_decide` 只往里写、不负责把它拼进返回值。
    """
    ledger = ctx["ledger"]
    rounds = int(state.get("plan_rounds") or 0)
    messages = list(state.get("messages") or [])

    # ⓪ 确认轮（生产 `graph._planner_preflight` 的同一格，20261019 接上）：主人在确认框上
    #    点了确定 ⇒ **零 LLM 照签名令牌拼计划**。判据与两条分支的理由在
    #    `agent.react_parity.confirm_round` 的头注里（那一份是唯一事实源）。
    #
    #    位置照生产：**在预算判据之前**。理由不是"顺序好看"——确认轮是"这句话已经被主人
    #    签过字了"，它不该被任何"这一轮还该不该再决策"的预算判据挡下；而受阻那一支
    #    （`rounds > 0`）本来就是靠 `_wrap_up_plan` 收尾，与预算收尾是同一件事、措辞不同。
    _cf = confirm_round(state.get("confirm_grant"), rounds,
                        bool(state.get("blocked")), messages)
    if _cf is not None:
        _plan, _ev, _payload = _cf
        record("planner", _ev, **_payload)
        ctx["decided"] = "execute" if plan_has_tools(_plan) else "model"
        return _plan_update(_plan, rounds)

    # ① 预算判据（原 `ConvergenceMiddleware.before_model`）：撞预算的那一轮**照样执行**，
    #    停的是"再来一轮"——判在拿到模型输出之后会把模型已经点名的调用整个吞掉（用户
    #    等了半天，动作没发生）。
    if convergence_verdict(ledger, rounds=rounds, budget=BUDGET)["kind"] == "budget":
        return _stop_update(state, ctx, "budget")

    # ② 首轮的五条确定性快道（生产 `rounds == 0 and not has_frames and fastpath_on`
    #    同一条守卫）。命中即拿走这一轮的决策权——**零 LLM**。
    #
    #    ⚠️ **守卫里的"有没有帧"必须读本轮窗口**（20261019 修，资产对齐 C）：这里此前
    #    读的是整条 `state["messages"]`，而它含 DB 历史——历史里只要有一条 ToolMessage
    #    （生产注入的 20 条里完全可能有），这个守卫就**恒假**，五条快道在多轮会话里
    #    一条都命中不了。生产读的是 `_has_frames(_turn(state))`（`graph.py:6093`），
    #    同一件事、只是坐标来自冻结的 `turn_start`；本臂没有那个坐标，用"最后一条
    #    HumanMessage 之后"（`_turn_tail`），与 `_window` 同一切法。
    #
    #    ⚠️ **`advance` 与生产对齐（不再 `advance=False`）**：生产那五条出口用的都是
    #    `_plan_update(x, rounds)`（默认 advance=True）。本臂此前写 `advance=False`，
    #    理由是"零 LLM 出口不该吃预算"——那句话出自 `graph._plan_update` 的 docstring，
    #    但**生产实际没这么做**，而且 `advance=False` 的合法前提是"配一个进度守卫"
    #    （同 `_plan_exec_next_card` 的 `plan_task_id`/`plan_cursor`）。快道没有守卫：
    #    一条产出零工具的快道（导航到不存在的页面 ⇒ 只有注记、没有帧）执行完
    #    `rounds` 没动、帧也没有 ⇒ 下一轮**同一条快道再命中一次**，原地打转。
    #    照生产写 = 同时修掉这个潜在死循环。
    if rounds == 0 and not _has_frames(_turn_tail(state)):
        _fp = fast_path(state, user_msg=ctx["user_msg"], page_ctx=ctx["page_ctx"])
        if _fp is not None:
            _plan, _kind = _fp
            logger.info("[react_arm] %s 快道命中（零 LLM）: %s", _kind, _plan.get("tools"))
            record("planner", "fastpath", kind=_kind, round=rounds,
                   tools=[str(t) for t in (_plan.get("tools") or [])])
            ctx["decided"] = "execute" if plan_has_tools(_plan) else "model"
            return _plan_update(_fast_plan(_plan), rounds)

    # ③ 一次决策。`bind_tools` **不传 `parallel_tool_calls`**（生产 planner 那边是
    #    `parallel_tool_calls=False`）：本线容忍一轮多条调用，合并成一份 `plan_obj`、
    #    逐条发 `execute` ⇒ 偏差表现为"步子更碎"，不会丢调用（见模块头注）。
    try:
        llm = _bound_llm(state, ctx)
        window = _window(state, ctx)
        _t0 = time.monotonic()
        resp = llm.invoke([SystemMessage(content=_prompt_of(state, ctx))] + window)
        # 用量落 trace（见 `_record_llm`）：`engine="native"` 与生产 `planner.llm_done`
        # 同档；`corrected` 取的是**这一次提示词里带没带纠偏**，所以必须在下面清空
        # 那一行**之前**读。
        _record_llm("planner", resp, _t0, engine="native",
                    corrected=bool(ctx.get("correction")))
        # 纠偏提示**只喂一次**，喂在**提示词那一格**（`{correction}`，生产
        # `_planner_setup` 同款）——所以清空必须在 `_prompt_of` **之后**。
        # 清在它之前（20261019 前）时那一格恒是缺省语：纠偏文本一个字节都没进过
        # 提示词，而"纠偏一次"这条判据照旧烧掉一轮 ⇒ 模型被问同一个问题两次。
        ctx["correction"] = ""
    except Exception as e:  # noqa: BLE001 —— 崩溃也要给主人一句诚实的话（见《五》）
        logger.warning("[react_arm] planner 调用异常，按诚实收尾处理：%s", e,
                       exc_info=True)
        ctx["wrap_up"] = _crash_text(ledger, e)
        ctx["decided"] = "model"
        return _plan_update(_wrap_up_plan(_has_frames(_turn_tail(state)),
                                          reason="内部错误"),
                            rounds, advance=False)
    ctx["draft"] = resp

    # ④ 无进展 / 意图纠偏（原 `ConvergenceMiddleware.after_model`）。
    verdict = convergence_verdict(ledger, rounds=rounds, budget=BUDGET, last_ai=resp,
                                 user_msg=ctx.get("user_msg") or "",
                                 corrected=bool(ctx.get("corrected")))
    if verdict["kind"] == "no_progress":
        return _stop_update(state, ctx, "no_progress")
    if verdict["kind"] == "correct":
        ctx["corrected"] = True
        ctx["correction"] = _correction_text(verdict["pending"])
        ctx["decided"] = "planner"      # 打回自己重决策**一次**
        return {}

    # ⑤ 零工具轮 = 模型自己宣布收尾（ReAct 里没有别的含义）⇒ 正文交给 `model` 的
    #    narrator。**不写计划**：与旧臂的 `last_plan` 同义——state 里留着上一轮那份计划，
    #    narrator 与闸门读到的就是"最近一次真计划"（`_norm_state` 再归一，空则补 chat）。
    if not (getattr(resp, "tool_calls", None) or []):
        ctx["decided"] = "model"
        return {}

    # ⑥ 写目标拒绝链（生产 `_decide_write_guard`，20261019 接上）：引号片段 / 名字值 /
    #    待办正文 / 目标出处 / 目标 / 台账编号 / 政策，命中即**零写 + 如实收尾**。
    #
    #    位置照生产：在**计划成形之后**（`_decide_instantiate` 的下一位）。它要读的是
    #    模型真填进来的那些参数，所以必须在 `_plan_of` 之后；而"零工具轮"那一支在它
    #    之前就返回了（那一轮没有目标可拒）。
    #
    #    这一格此前**整条不存在**（本臂 0 引用）：形状是"主人没说过那个名字、模型编了
    #    一个，本臂照样把它写进站内"——而 `forbid_*` 那一族负断言在"什么都不做"的臂上
    #    恒真，所以当年的读数并不作废、只是**没在比同一件事**。
    _frames = _has_frames(_turn_tail(state))

    # ⑥a 伪函数摘除 + 任务台账登记（生产 `_planner_decide` 的 D1 尾 / D3 两段，20261019
    #     接上）。**顺序照生产**：摘伪函数 → 自动登记（意图清单 − 本轮办了的）→ 显式登记
    #     → 实例化。这一格此前**整条不存在**，而它缺失的形状是**静默**的：
    #     `task_hold`/`task_drop`/`task_intents` 三个伪函数**常绑在菜单里**，模型发得出来，
    #     而 `_plan_of` 拿名字去 `instantiate_plan`，那三个不在 `WORKFLOW_MAP` 里 ⇒
    #     静默回退成 `chat`、零工具、零注记（实测三种名字都是 `chat [] ''`）。
    #     ⇒ 模型说"这件事我记下了"，系统读成闲聊，而**没有任何东西会红**。
    #
    #     ⚠️ **落点必须在 `_plan_of` 之前**：`_decide_register` 命中时会把整份计划换成
    #     收尾计划（"这件事我记下了，这一轮到此为止"），在它之后再实例化等于把那份收尾
    #     覆盖掉——那一轮会照常去执行模型点的技能，而主人刚被告知"记下了"。
    _rest, _decided, _early = _task_calls(resp, ctx, rounds, _frames)
    if _early is not None:
        return _early
    _plan = _plan_of(_rest, ctx.get("role")) if _rest else _chat_plan(ctx.get("role"))
    _plan, _early = _register_stage(state, config, ctx, sink, _decided, _plan,
                                    rounds, _frames)
    if _early is not None:
        return _early
    _refused = write_guard(state, ctx=ctx, plan_obj=_plan, rounds=rounds,
                           has_frames=_frames)
    if _refused is not None:
        # 收尾那一轮：`decided="model"` ⇒ 去 narrator 拿着那段 note 如实说"没做"。
        # 与生产同（`_Step("return", _plan_update(c.plan_obj, c.rounds))`）。
        ctx["decided"] = "model"
        return _plan_update(_refused, rounds)

    # ⑥b 剔空纠偏（生产 `_decide_late_correct` 的最后一格，20261019 接上）——判据与
    #     现场见 `_drop_correct_stage`。
    _dc = _drop_correct_stage(ctx, _plan, rounds)
    if _dc is not None:
        return _dc

    # ⑥c 零工具轮的确定性终局（生产 `_finalize_unaccounted` 的三格，20261019 接上）：
    #     剔空纠偏之后仍然零工具 / 参数不齐 / 记账字段全空——三种形态都**不该交给
    #     narrator 自己编**（它手里一帧都没有，只能编）。命中即换成带禁止句的收尾计划。
    #     位置照生产：在决策循环之外、`_finalize_dedupe` **之前**。
    _unacct = unaccounted(_plan, rounds, _frames)
    if _unacct is not None:
        ctx["decided"] = "model"
        return _plan_update(_unacct, rounds)

    # ⑦ 收尾去重/拦截（生产 `_finalize_dedupe`，20261019 接上）：字面路径修正 + 八条
    #    守卫（动作重复执行防护、已读裁剪、零改动重复、已办写收尾……）。它在生产里
    #    跑在决策循环**之外**、`_finalize_wrap` 之前，位置照搬。
    #
    #    ⚠️ 它就地改写 `plan_obj["tools"]`（甚至整份换掉），所以**必须**跟着调
    #    `_sync_calls`——否则判据的结论落不进 execute 拿的那份清单。
    _plan, _ended = finalize_dedupe(state, ctx=ctx, plan_obj=_plan, rounds=rounds,
                                    has_frames=_frames,
                                    turn_messages=_turn_tail(state))
    if _ended:
        # 守卫命中即终局（生产那八条出口都是 `return`）。计划里有工具就照常执行、
        # 没有就去 narrator——与生产 `route_after_planner` 同一条判据。
        ctx["decided"] = "execute" if plan_has_tools(_plan) else "model"
        _sync_calls(_plan)
        return _plan_update(_plan, rounds)

    # ⑧ 出口路由（20261019 修，资产对齐 J）：**零工具 ⇒ 去 narrator，不是去 execute**。
    #    判据与生产 `graph.route_after_planner` 逐字同（`"execute" if plan["tools"]
    #    else "model"`），也与本节点上面三条出口（确认轮 / 快道 / 去重终局）逐字同
    #    ——它们写的一直是 `plan_has_tools(_plan)`，只有这最后一条硬写着 `"execute"`。
    #
    #    ⚠️ **这一格此前是一个死循环**（20261019 实测，纯 `chat` 轮）：零工具的出口去
    #    execute ⇒ execute 逐条跑 `calls`（空的）⇒ 什么都没做 ⇒ `route_after_execute`
    #    见"无受阻项"又回 planner ⇒ planner 再问一次模型 ⇒ 同样零工具 ⇒ …… 直到撞
    #    `MAX_PLAN_ROUNDS` 才收尾。实测形状：一句"你好"要 **4 次决策调用 + 4 次 execute**，
    #    而最后那次叙述是在预算用完之后才发生的。代价是三重的——钱（每轮多问 3 次）、
    #    时间（每轮多三次往返）、以及**读数**（模型被问同一句话四遍，四遍的答案可以不同，
    #    而 golden 只看最后一遍）。
    #
    #    为什么它会存在：`8a82b83` 把第二条臂从 `create_agent` + 中间件换成真 StateGraph
    #    时，中间件那版"零工具轮就是收尾轮"是**结构性**的（循环自己会停），拆成显式节点
    #    之后这条边要有人写出来——上面三条出口写对了，这一条漏了。**20261012 那次对照
    #    跑的是旧形状**，所以这个形状从来没有被比过。
    _sync_calls(_plan)
    ctx["decided"] = "execute" if plan_has_tools(_plan) else "model"
    return _plan_update(_plan, rounds)


def _task_calls(resp, ctx: dict, rounds: int,
                frames: bool) -> tuple[list, object, dict | None]:
    """⑥a 前半：伪函数摘除 + 注入参数摘除 + 记账 + "点了菜单外名字"的响亮收尾。

    → `(真调用, decided, 提前返回的更新或 None)`。第三个非空时调用方**直接返回**它。

    为什么要拆成函数（不是为了好看）：这一整段的每一条出口都是"模型发了系统收不下的
    东西"，而**每一格的后果都不同**——伪函数参数无效要记账继续走，点了菜单外的名字要
    确定性收尾。混在 `_planner_decide` 里会让那个函数的分支数越过复杂度闸（`C ≤ 30`），
    而闸的作用正是"读到第 40 个分支的人已经看不出漏了哪条"。
    """
    rest, declare, intents, notes = split_pseudo_calls(resp.tool_calls, ctx.get("role"))
    rest, intents = strip_injected_args(rest, ctx.get("role"), intents)
    if notes:
        # 生产把这几笔记进 `PlannerDecision.notes`（trace）。本臂**响亮**：`notes` 里每
        # 一格都是"模型发了伪函数而系统收不下"（`task_hold_invalid` 之类），静默收下就
        # 等于把"这次登记无效"伪装成"这轮没有登记"。
        record("planner", "task_note", round=rounds, notes=list(notes))
        logger.warning("[react_arm] 任务伪函数记账：%s（round %d）",
                       "、".join(notes), rounds)
    bad = unknown_workflows(rest, ctx.get("role"))
    if bad:
        # 生产 `tool_calls_to_plan` 在"点了不在本轮 schema 里的函数名"时返回 None ⇒
        # 确定性收尾。本臂此前让这些名字落进 `instantiate_plan` 的 `or WORKFLOW_MAP["chat"]`
        # ⇒ "点了个菜单里没有的函数"与"这轮决定闲聊"**长得一模一样**。
        record("planner", "native_fallback", round=rounds, finish="",
               disposition="unknown_tool", names=bad)
        logger.error("[react_arm] 模型点了不在本轮菜单里的函数名：%s → 确定性收尾"
                     "（本轮零执行）", "、".join(bad))
        ctx["decided"] = "model"
        return rest, None, _plan_update(
            _wrap_up_plan(frames, reason="本轮模型点了菜单里没有的函数名，"
                                         "没有得到可执行的决策"), rounds)
    # `workflow` 取**第一条真调用**的技能名（生产 `PlannerDecision.workflow` 就是
    # `calls[0].name`）；纯伪函数轮没有真调用 ⇒ `chat`（生产那条 `if not calls:` 出口同款）。
    head = str((rest[0] or {}).get("name") or "") if rest else "chat"
    return rest, task_decision(declare, intents, head), None


def _register_stage(state, config, ctx: dict, sink: list, decided, plan_obj: dict,
                    rounds: int, frames: bool) -> tuple[dict, dict | None]:
    """⑥a 后半：自动登记（意图清单 − 本轮办了的）+ 显式登记（`_decide_register`）。

    → `(plan_obj, 提前返回的更新或 None)`。两条出口各走各的通道，与生产逐字同：

      · `"return"`（显式登记收尾）—— `_decide_register` 自己把 `frame_payload` 那一帧
        拼进 `_plan_update` 的 `task_frames`，这里原样带出去；
      · `"continue"`（只登记、零工具零问题）—— 走纠偏通道重决策**一次**，返回 `{}`
        （**不吃轮数**：它没花一次新的 LLM 决策）。`ctx["corrected"]` 与 ④ 的意图纠偏
        **共用**——生产也是同一个槽（`c.correction`）。

    自动登记那几帧写进壳持有的 `sink`（**覆盖**语义，见下面那句注），由
    `planner_node` 在返回前并进去。
    """
    auto = auto_task_frames(state, config, decided, rounds)
    if auto:
        # 覆盖语义（与生产那处 `c.task_sink[:] = _intent_frames_now` 逐字同）：纠偏会
        # 重决策一次，"只交清单"那一格的重决策正是被摘掉清单逼去点技能的 ⇒ 那一版的
        # `intents` 必然为空，`extend` 等于把刚收下的枚举留下**两份**（同一个 goal 两帧）。
        sink[:] = list(auto)
    new_plan, action, correction, explicit = register(
        state, ctx=ctx, plan_obj=plan_obj, rounds=rounds, has_frames=frames,
        decided=decided)
    if action == "continue":
        # `planner.task_correct` **不在这里记**：`_decide_register` 自己记了一条（带
        # `goal`/`steps`/`round` 三格，`eval/corpus_invariants.py` 读的就是它）。
        # 这里再记一条就是同一个事件两个产地——两处的 `round` 语义一旦漂开（一个数
        # "纠偏前"、一个数 "纠偏后"），读数会变成"纠偏发生了两次"。
        ctx["corrected"] = True
        ctx["correction"] = correction
        logger.warning("[react_arm] 只登记任务、零工具零问题 → 纠偏重决策一次：%s",
                       (getattr(decided, "declare", None) or {}).get("goal"))
        ctx["decided"] = "planner"
        return plan_obj, {}
    if action == "return":
        ctx["decided"] = "model"
        return new_plan, _plan_update(new_plan, rounds, explicit)
    return new_plan, None


def _drop_correct_stage(ctx: dict, plan_obj: dict, rounds: int) -> dict | None:
    """⑥b 剔空纠偏：点名的工具**一个都没执行** ⇒ 纠偏重决策一次。

    → 命中时返回**提前返回的那个更新**（`{}` = 打回自己重决策一次）；不命中 ⇒ `None`。
    调用方判 `is not None`（不能判真假：命中时返回的正是空 dict）。

    现场（20260921 22:34，见 `react_parity.drop_correction`）：管理员问某篇草稿的标签，
    planner 点名 `list_admin_notes` **意图对路**，但那工具不在 content_query 的白名单里
    ⇒ 清单被剔空 ⇒ 旧行为当收尾轮处理 ⇒ narrator 零帧编话 ⇒ gate 打回 ⇒ 主人看到一句
    降级回复**且这一轮直接结束**（原话："居然就直接结束而不是重新规划执行"）。

    位置照生产：在**决策循环的尾巴上**（拒绝链之后）。`ctx["corrected"]` 是"只纠一次"
    的那一格（与 ④ 的意图纠偏**共用**同一个旗子——生产也是同一格 `c.correction`
    非空即不再纠，两条纠偏互斥）。

    为什么单起一个函数：`_planner_decide` 的分支数压在复杂度闸（`C ≤ 30`）上，而闸的
    作用正是"读到第 40 个分支的人已经看不出漏了哪条"。
    """
    if not (plan_obj.get("dropped") and not (plan_obj.get("tools") or [])):
        return None
    if ctx.get("corrected"):
        return None
    ctx["corrected"] = True
    ctx["correction"] = drop_correction(plan_obj["dropped"], ctx.get("role"))
    record("planner", "drop_correct", dropped=plan_obj["dropped"], round=rounds)
    logger.warning("[react_arm] 点名工具全被剔除（本轮零工具）→ 剔空纠偏重决策：%s",
                   "、".join(str(d) for d in plan_obj["dropped"]))
    ctx["decided"] = "planner"      # 打回自己重决策**一次**
    return {}


def _sync_calls(plan_obj: dict) -> None:
    """把**定稿后的** `plan_obj["tools"]` 同步回 `calls[*].specs`（就地改）。

    **为什么非同步不可**：`calls` 是"这一轮做什么"从 planner 交到 execute 的唯一载体
    （见 `planner_node` 的 docstring），而 execute 对**带 `specs` 的**调用走
    `run_specs`（清单已定稿）。planner 阶段有好几处会**就地改写** `plan_obj["tools"]`：
    写目标拒绝链里的三条校正（引号片段 / 目标名 / 名字值）、`_finalize_dedupe` 的字面
    路径修正与三条裁剪。不同步的话，形状是"判据算了、trace 落了、而执行用的是改写之前
    那份"——系统明明把名字校正回来了，写进站内的还是模型编的那个。这正是本批要消灭的
    那类静默（**判据在，产物不在**）。

    认领规则：`len(calls) == 1`（生产的形状，本臂也基本如此）⇒ 整份定稿清单给它。
    多调用那一支是本臂的容差（`_plan_of` 的并集），按**工具名在原序里**贪心认领
    ——每条调用只认自己那几种工具，先到先得。**认领不到就炸**：清单里有工具不属于
    任何一条调用，意味着"这一条帧是谁发的"没有答案，而那个答案驱动
    `_pair_frames` 的合成 assistant（错配的后果是出站序列非法）。
    """
    calls = list(plan_obj.get("calls") or [])
    final = [str(t) for t in (plan_obj.get("tools") or [])]
    if not calls:
        # **计划被重建过**（`_name_arg_fix` 那类校正是 `plan_obj.clear() + update(fresh)`，
        # 而 `fresh` 是 `instantiate_plan` 的产物、没有 `calls` 这一格）⇒ 本臂那个键
        # 整格消失了。不补的话形状是"判据把名字校正回来了、计划里也写着，而 execute
        # 读到零条调用 ⇒ 零执行、不弹卡、不报错"——一次写就这么静默消失。
        # 重建后的计划是**单技能**的一份（校正只可能产自一次调用），所以这里补一条。
        if not final:
            return
        plan_obj["calls"] = [{"workflow": str(plan_obj.get("workflow") or ""),
                              "params": dict(plan_obj.get("params") or {}),
                              "specs": final}]
        return
    if len(calls) == 1:
        calls[0]["specs"] = final
        return
    # 认领按**一个名字一个坑**逐个走，不是"把这一条调用那几种工具全捞过来"：
    # 同一轮里点两条同工具调用（`[toggle_effect(off), toggle_effect(on)]` 就是
    # "把樱花换成下雨"最自然的形态）时，"按名字集合捞"会让**第一条调用把两条都吃掉**、
    # 第二条只剩空清单 ⇒ 它退回 `run(workflow, params)` 重新实例化，凭空多跑一次。
    wanted = [[_tool_name(s) for s in (c.get("specs") or [])] for c in calls]
    taken = [False] * len(final)
    for c, names in zip(calls, wanted):
        mine: list[str] = []
        for nm in names:
            for i, spec in enumerate(final):
                if not taken[i] and _tool_name(spec) == nm:
                    taken[i] = True
                    mine.append(spec)
                    break
        c["specs"] = mine
    left = [final[i] for i, t in enumerate(taken) if not t]
    if left:
        raise RuntimeError(
            f"_sync_calls 认领不到这些定稿 spec：{left}"
            f"（calls={[c.get('workflow') for c in calls]}）")


def route_after_planner(state: AgentState, config: RunnableConfig | None = None) -> str:
    """`planner` 的下一站（判据是**本节点这一轮的决定**，见 `_plan_update` 的三条出口）。"""
    return PLANNER_ROUTES.get(str(_maybe_ctx(config).get("decided") or "model"), "model")


# 路由表标 `Hashable` 而不是 `str`：`StateGraph.add_conditional_edges` 的形参就是
# `dict[Hashable, str]`，而 dict 的键是**不变**的 ⇒ `dict[str, str]` 传不进去（mypy 判红）。
PLANNER_ROUTES: dict[Hashable, str] = {"planner": "planner", "execute": "execute",
                                       "model": "model"}


# ══════════════════════════════════════════════════════════════════════════════
# execute 节点：跑清单（真工具调用 + 回执行 + 弹卡）
# ══════════════════════════════════════════════════════════════════════════════

def _popup_update(state: AgentState, ctx: dict) -> dict:
    """同意闸上攒下的写 spec → 生产的确认卡（`graph._confirm_popup`，**同一个资产**）。

    「触发逻辑照搬」在这里的实现方式是**直接调用它**，不是抄一份：那里面的排序
    （免弹窗的三条前提 → 硬权限 → 参数/`$ref` → 文章目标有据 → 惰性快照 →
    `reached_specs` 滤空）改一处漏一处就是一条新漂移源，而它恰好是本仓最常见的缺陷。
    它能被这么用的理由是它读的每一格 `AgentState` 都有真值——批 1 之前本线喂的是
    `frames.popup_state` 拼的最小 state，那张表自称四格、AST 走完实测**十一格**，
    漏掉的 `executed` 正是「令牌用在那一件上就作废」那一格 ⇒ 那条判据恒不生效。

    返回 `{}` = 这批不该弹卡（主人这句是提问、参数里还挂着 `$ref`、目标不在主人原话里
    ……都是 `_confirm_popup` 里的 `continue`）。此时这批 spec **既没执行也没卡**，与生产
    同形。判过即清 `pending`：同一批 spec 不该被两张卡问两遍。
    """
    ledger = ctx["ledger"]
    if not ledger.pending:
        return {}
    pends = list(ledger.pending)
    ledger.pending.clear()
    specs = [str(p.get("spec") or "") for p in pends]
    try:
        popup = _confirm_popup(state, specs, ctx.get("principal"),
                               ctx.get("user_msg") or "", ctx.get("config"))
    except Exception as e:  # noqa: BLE001 —— 弹卡判据炸了不该把整轮吃掉
        logger.warning("[react_arm] _confirm_popup 抛异常，本轮按不弹卡处理：%s", e,
                       exc_info=True)
        return {}
    if not popup:
        return {}
    kind = str(popup.get("kind") or "")
    if kind == "confirm":
        record("execute", "consent_popup", principal=str(ctx.get("principal")),
               specs=[str(p.get("tool") or "") for p in pends])
        logger.info("[react_arm] 写操作未判成命令 → 弹确认框（零执行）: %s",
                    [p.get("tool") for p in pends])
    elif kind == "noop":
        record("execute", "noop_exit", via="react_arm")
        logger.info("[react_arm] 状态已达成 → 走零改动出口（不弹卡、零执行）")
    else:
        # 认不出的判别键 = 生产端与消费端漂移，**响亮失败**（同 `graph.execute_node`
        # 那条）：宁可这一轮如实收尾，也不拿一个来路不明的 dict 当弹卡批次发出去。
        logger.error("[react_arm] 确认出口的判别键认不出（kind=%r，键=%s）→ 按零改动收尾",
                     kind, sorted(popup))
        return {"noop_text": NOOP_TEXT,
                "noop_note": f"确认出口判别键认不出（{kind or '空'}），本轮零改动"}
    # `kind` 只是判别键，**不进 state**（`AgentState` 里没有它，塞了会被静默丢掉）。
    return {k: v for k, v in popup.items() if k != "kind"}


def execute_node(state: AgentState, config: RunnableConfig | None = None) -> dict:
    """跑清单：逐条技能 → `WorkflowExecutor.run`（展开 → 权限 → 同意闸 → checker 验收）。

    帧的顺序与生产同源：**工具帧（`messages`）先到，本节点的 `updates` 后到**
    （`graph.execute_node` 也是先产 ToolMessage 再交回 update）。
    """
    _guard(config)
    ctx = _ctx(config)
    ledger = ctx["ledger"]
    executor = ctx["executor"]
    # 令牌作废那一格（`_grant_covers` 的 `executed`）读的是"本轮已执行过什么"：生产由
    # `execute_node` 在请求内累积 state，本线把它指到**当前**那份 state 上（每轮都是新的
    # 一份），再与 `executor.calls` 相加。
    executor.state = state
    calls = list((state.get("plan_obj") or {}).get("calls") or [])
    out: list = []
    # 记下"这一批清单是谁发的"（`_pair_frames` 靠它把工具帧还原成带真参数的合成
    # assistant）。**在 try 之前、按清单原序**登记：异常路径下已发出的那几条帧也认得出
    # 出处。`base` 是本批在下标空间里的起点（`origin` 的值是**调用**的下标，不是帧的
    # ——一次技能调用展开成几条帧是 `instantiate_plan` 的事，与模型说了几条无关）。
    base = len(ctx["calls"])
    ctx["calls"].extend({"name": str((c or {}).get("workflow") or ""),
                         "args": dict((c or {}).get("params") or {})} for c in calls)
    try:
        for ci, c in enumerate(calls):
            workflow = str((c or {}).get("workflow") or "")
            n_before = len(executor.calls)
            # 清单**已定稿**的调用（确认轮的签名体、特效切换快道的双 spec）走
            # `run_specs`：它们的产出**本身就是**计划，再实例化一次会把它改坏
            # （理由见 `react_line.WorkflowExecutor.run_specs` 的 docstring）。
            # 判据是"清单**非空**"而不是"`specs` 在不在"：定稿清单为空时走 `run`，是
            # 为了拿它零工具出口那两句（`note` / `reply` + 回执行）——导航到不存在的
            # 页面那类**只有注记、没有帧**的计划，注记就是全部内容，走 `run_specs` 会
            # 得到空串（实测：那一格此前静默丢过）。
            # 而"定稿为空 ⇒ 重跑一遍实例化会改坏它"这件事在这里**不成立**：没有工具
            # 就没有"实例化出来的东西与定稿的不一样"这回事。
            _final = [str(s) for s in ((c or {}).get("specs") or [])]
            _params = dict((c or {}).get("params") or {})
            # `sig` **必须由这里传**：无进展判据的键取在字面路径修正**之前**、按
            # `(workflow, params)` 算，而 `run_specs` 缺省时是从 `{"specs": [...]}` 算的
            # ——两把键不同源 ⇒ `convergence_verdict` 与台账里那条永远对不上，"同一件事
            # 原地重做"这一族恒判"有进展"（形态是模型拿着没进展的轮次反复重规划，而
            # 判据从不触发）。`run` 那条路自己算同一把键，所以这里传的就是它原来那个值。
            _sig = spec_signature(workflow, _params)
            text, n_tools = (executor.run_specs(workflow, _final, sig=_sig) if _final
                             else executor.run(workflow, _params))
            if not n_tools:
                # 纯应答技能（chat 一类）：零工具 ⇒ **不发帧**（生产那一轮
                # `if not specs: return {"messages": []}` 同款）。结果只喂模型
                # （合成注记帧，见 `_window`）——它不进 `state["messages"]`。
                ctx["notes"].append({"workflow": workflow, "text": text})
                continue
            for call in list(executor.calls[n_before:]):
                # `_frame_id` 的基数是"消息里已有的帧数"，而这一批里刚发的那些还没进
                # state ⇒ 用**本节点内的序号**（`len(out)`）递增，同一批里也不会撞。
                tid = _frame_id(state.get("messages") or [], len(out))
                ctx["origin"][tid] = base + ci
                line = str((call or {}).get("line") or "")
                if line:
                    ctx["lines"][tid] = line
                out.append(ToolMessage(content=str((call or {}).get("result") or ""),
                                       name=str((call or {}).get("tool") or ""),
                                       tool_call_id=tid))
    except Exception as e:  # noqa: BLE001 —— 见《五》：崩溃照样产诚实收尾
        logger.warning("[react_arm] execute 异常，按诚实收尾处理：%s", e, exc_info=True)
        ctx["wrap_up"] = _crash_text(ledger, e)
        return _exec_body(out, ledger, executor)

    body = _exec_body(out, ledger, executor)
    body.update(_popup_update(state, ctx))
    return body


def _exec_body(out: list, ledger: RunLedger, executor: WorkflowExecutor) -> dict:
    """`execute` 的公共返回（崩溃支与正常支逐字同一形状）。"""
    return {"messages": out,
            "receipts": list(ledger.receipts),
            # 参数引用的取值源（生产 `execute_node` 的同一格，请求内累计）。本轮
            # 提示词的 `{ref_hints}` 槽读的就是它——不回写 ⇒ 那一槽永远说"无从引用"，
            # 而模型照提示词写不出引用、多步技能只能靠它把上一步的 id 抄一遍
            # （抄错就是另一个病）。形状（`{"tool","data"}`）由 `react_line` 保证。
            "tool_data": list(executor.tool_data),
            # 零改动签名（生产 `AgentState.noop_specs` 的同一格）：planner 的零改动重复
            # 裁剪读它。**必须显式回写**——未声明的键会被 langgraph 静默丢出 updates
            # 流，那一格就恒为空表、判据静默失效（这正是该字段在 AgentState 里那条
            # 长注写着的坑）。
            "noop_specs": list(executor.noop_specs),
            # `blocked_seen` 的键形与生产 `execute_node` 的 `_blocked_key` 逐字同款
            # （`工具::原因码`）——闸门与账甲读它。
            "blocked": [_blocked_row(b) for b in ledger.blocked],
            "blocked_seen": sorted({f"{b.get('tool')}::{b.get('reason')}"
                                    for b in ledger.blocked}),
            "executed": executed_specs(executor.calls)}


def route_after_execute(state: AgentState, config: RunnableConfig | None = None) -> str:
    """`execute` 的下一站（与生产 `graph.route_after_execute` 同一条判据）。

    弹卡轮 / 零改动轮**到此为止**——生产那边 `narrator 结构上不会跑`，本线走同一条边
    ⇒ 不可能出现"已经建好啦"这类叙述。正文由 producer 用 `confirm_text` / `noop_text`
    发**一次**（臂这边一个字节都不补，补了就是两遍）。
    """
    if state.get("pending_confirm") or state.get("noop_note"):
        return "end"
    if _maybe_ctx(config).get("wrap_up"):
        return "model"          # 崩溃收尾：不再回 planner
    return "planner"


EXECUTE_ROUTES: dict[Hashable, str] = {"planner": "planner", "model": "model",
                                       "end": END}


# ══════════════════════════════════════════════════════════════════════════════
# model 节点：narrator
# ══════════════════════════════════════════════════════════════════════════════

def model_node(state: AgentState, config: RunnableConfig | None = None) -> dict:
    """收尾叙述。三条出口（见模块头注《决策·四》与三条铁律①）：

      ① **确定性正文路**（收敛判据的收尾语 / 崩溃语）：不调 narrator，直接发一片
         `AIMessageChunk`——正文只有分片这一条通道，`_chunk` 的 docstring 解释了
         为什么必须是分片类型；
      ② **正常路**：调 narrator（**真流式**）⇒ 返回**原始响应对象**，分片由 LLM 流给。
         返回原对象而不是重建一条：它的 id 已经被 `messages` 通道认领过
         （`on_llm_end` 的 dedupe 靠它），重建 = 同一段正文发两遍；
      ③ **narrator 失败退回草稿**：同 ①，包成 `AIMessageChunk`。草稿是决策模型那条
         零工具消息——一条有内容、没经过 narrator 资产的话，仍然比"我刚才好像卡住了"
         诚实（见 `_narrator_call` 的注）。
    """
    _guard(config)
    ctx = _ctx(config)
    if ctx.get("wrap_up"):
        return {"messages": [_chunk(str(ctx["wrap_up"]))]}
    reply = _narrate(state, ctx)
    if reply is not None:
        return {"messages": [reply]}
    draft = ctx.get("draft")
    text = repair_sticker_tokens(str(getattr(draft, "content", "") or ""))
    if not text.strip():
        # 空收尾（模型给了个空消息）：**不发帧**。`nonempty` 断言会因此红——那是
        # **真的**（主人确实没读到东西），不该由本层补一段话来掩盖。
        return {"messages": []}
    logger.warning("[react_arm] narrator 两次都没给出正文，退回决策模型那条草稿")
    return {"messages": [_chunk(text)]}


def _narrate(state: AgentState, ctx: dict) -> Any:
    """最终答复由**独立的零工具 narrator** 写（P6 缺口的闭合点，见模块头注）。

    **为什么必须独立**：闸门判的是**叙述的声称**与执行事实对不对得上，而生产那个被判的
    对象有一个结构性质——它是**零工具节点**（`model_node` 不 `bind_tools`），"我调用了 X"
    这句话从它嘴里出来必然是声称，不可能是事实。本线此前是**同一条消息流里决策模型自己
    写收尾**：它手上就有工具，同一句话既可能是回忆也可能是谎言，判据判的东西就与生产不是
    同一种了。拆开之后三件事各归各位：决策模型只决定"够了没有"，narrator 只负责说，
    闸门只判后者——两臂才第一次可比。

    **草稿不进提示词**（与生产同形）：生产 narrator 看不到任何"草稿"，它读的是计划契约与
    工具帧。本线把决策模型那条零工具消息当作"这一轮到此为止"的信号，正文**重写**。

    异常**吞掉**（返回 `None` ⇒ 调用方退回草稿）：narrator 是加装的一层，它自己炸了不该把
    主人这一轮的答复整条吃掉（与 `gate` 节点那条同一条理由）。
    """
    receipts = [r for r in (state.get("receipts") or []) if isinstance(r, dict)]
    drop = {str(r.get("tool") or "") for r in receipts if is_block_family(r)}
    try:
        _head, _tail = _narrator_prompt(state, receipts, drop, ctx)
        # 装配顺序与生产 `model_node` 逐字同：`[稳定头] + [历史…（上下文块挪尾）] + [变化段]`。
        # `narrator_wire` 里那一趟 `with_tool_call_pairs` 就是此前这一行显式写的那个调用
        # ——收进生产那一份之后，"补形状"只剩一个产地。
        return _narrator_call([_head] + narrator_wire(list(state.get("messages") or []))
                              + [_tail])
    except Exception as e:  # noqa: BLE001 —— 见 docstring 最后一段
        logger.warning("[react_arm] narrator 调用异常，退回决策模型那条草稿：%s", e,
                       exc_info=True)
        return None


# ══════════════════════════════════════════════════════════════════════════════
# gate 节点：直调生产那一份
# ══════════════════════════════════════════════════════════════════════════════

def gate_node(state: AgentState, config: RunnableConfig | None = None) -> dict:
    """最终答复候选过一遍 `graph.gate_node`（**同一个资产，不抄第二份**）。

    **为什么必须直调它**：这条臂此前结构上不会发 fallback ⇒ `arm_capability` 判
    `comparable=False`（138 条 `forbid_fallback` 全白发），那 138 条断言连同它们保护的
    "不许把没做的事说成做了"一起失效。判据住在 `gate_node` 里（`_REPLAN_ISSUES` 约 20 族 /
    `_claim_issue` 的声称族 / `_repeat_reason` 的复读族 / 编造 URL / 零帧如实核验），
    **一条也不许在这里重写**。

    **重规划只此一次**，两处保证，与生产同源：`gate_node` 的 `fail()` 自己就有那条
    `not state.get("gate_replan")` 的判据；`route_after_gate` 再确认一次语义
    （`not done`）。`RemoveMessage` 由图自己生效——被否定的那条 AI 消息确实在 state 里。

    异常**吞掉并按 pass 处理**（`logger.warning` 留痕）：闸门是**加装**的一层，它自己炸了
    不该把主人这一轮的答复整条吃掉——生产那边 `gate_node` 抛异常会被 langgraph 变成整轮
    错误，本线没有那层兜底，所以这里自己兜。**这条口径差是明写的，不假装同源。**
    """
    _guard(config)
    try:
        return _prod_gate_node(_norm_state(state), config)
    except AgentCancelled:
        raise
    except Exception as e:  # noqa: BLE001 —— 见 docstring 最后一段
        logger.warning("[react_arm] gate_node 抛异常，本轮按「未打回」处理：%s", e,
                       exc_info=True)
        return {"done": True, "gate_replan": False}


def route_after_gate(state: AgentState) -> str:
    """与生产 `graph.route_after_gate` 逐字同款（含那条 `not done` 的第二道锁）。"""
    return "planner" if state.get("gate_replan") and not state.get("done") else "end"


GATE_ROUTES: dict[Hashable, str] = {"planner": "planner", "end": END}


# ══════════════════════════════════════════════════════════════════════════════
# 建图
# ══════════════════════════════════════════════════════════════════════════════

def build_graph() -> Any:
    """四个真节点 + 四条条件边（拓扑见模块头注）。"""
    g = StateGraph(AgentState)
    g.add_node("planner", planner_node)
    g.add_node("execute", execute_node)
    g.add_node("model", model_node)
    g.add_node("gate", gate_node)
    g.add_edge(START, "planner")
    g.add_conditional_edges("planner", route_after_planner, PLANNER_ROUTES)
    g.add_conditional_edges("execute", route_after_execute, EXECUTE_ROUTES)
    g.add_edge("model", "gate")
    g.add_conditional_edges("gate", route_after_gate, GATE_ROUTES)
    return g.compile()


# 图是"编一次、跨请求共用"的（见 `_CTX_KEY` 的注）——`build()` 每次调用都重编会让
# 测试套件里每条用例多付一次编译开销，而且没有必要（节点函数是纯的，状态全在
# state 与 config 里）。
_GRAPH: Any = None


def _compiled() -> Any:
    global _GRAPH
    if _GRAPH is None:
        _GRAPH = build_graph()
    return _GRAPH


# ══════════════════════════════════════════════════════════════════════════════
# 臂本体（`server._agent` 上的那个对象）
# ══════════════════════════════════════════════════════════════════════════════

class ReactGoldenArm:
    """`server._agent` 上的那一层薄外壳——**只剩三行**：建账、注入、转交。

    保留这个类名是硬要求：`tests/test_react_arm.py` ⑫ 断言装在 `server._agent` 上的
    对象类型名，`eval/arm_capability.py` 的 `_ForcedGate` 也按
    `.stream(state, config, stream_mode)` 包装。
    """

    name = ARM_NAME

    def __init__(self, graph: Any) -> None:
        self._graph = graph

    def stream(self, state: dict, config: RunnableConfig | None = None,
               stream_mode: object = None) -> Any:
        """接 `graph.graph_input(...)` 那份 state，产 `(mode, data)` 二元组。

        `stream_mode` 缺省给 `["messages", "updates"]`（生产者要的就是这两路）——收下
        这个参数只是为了让签名与图一致，签名对不上会以 `TypeError` 的形态炸在生产者里。

        ⚠️ **本臂刻意不收 `durability`**（20261018 批 7）：它不挂 checkpointer，
        持久化档对它毫无意义。生产者只在 `server._graph_holds_context()` 为真时才给这一格，
        而本臂恒为假 ⇒ 正常情况下永远收不到；**真收到就是接线错了**，那时 `TypeError`
        比"静默收下"好——静默收下等于把"这一臂没有持久化"伪装成"有持久化但没区别"。
        """
        src = dict(state or {})
        messages = list(src.get("messages") or [])
        # 输入归一：`graph_input` 的键集**恰好**等于 `AgentState` 的声明集
        # （`tests/test_state_keys.py` ⑤ 锁着），未声明的键会被 langgraph 静默丢掉。
        # 调用方（生产者）本来就给的是那一份；这里再归一一次是为了让**单测**那种
        # 只给 `{"messages": [...]}` 的调用也走同一条路（否则 `gate_node` 的
        # `state["messages"]` 之外那些格会 KeyError）。
        st = graph_input(messages, confirm_grant=src.get("confirm_grant"),
                         ledger=src.get("ledger") or {})
        for k, v in src.items():
            if k in AgentState.__annotations__:
                st[k] = v

        principal = _principal_of(config)
        role = principal.known_role
        # 「主人这一轮说了什么」只有**一个**来源（20261019 资产对齐 I）：规则与生产
        # `graph._intent_src` 逐字同——确认兑现轮（`confirm_grant` 在场）读的是
        # **主人原话**（倒数第二条 HumanMessage），不是前端在点「确定」时合成的卡面
        # 文本（「确认执行：…吗？点「确定」我就去办。」是**我们自己的话**）。
        #
        # 判错源的代价生产侧记过两次（见 `_intent_src` 的长注）：`_confirm_popup` 见
        # 问号判"像提问"⇒ **第二张卡根本不弹**；同意闸 `consent_granted(name, 卡面文本)`
        # 恒假 ⇒ 那一件写落成 `consent_required` BLOCK。臂这边同一处：`action_conflict`
        # （反极性改选）、写目标拒绝链的名字地基、剔空纠偏读的都是 `ctx["user_msg"]`。
        user_msg = ((_prev_user_msg(messages) or _last_user_msg(messages))
                    if src.get("confirm_grant") else _last_user_msg(messages))
        conf = dict((config or {}).get("configurable") or {})
        # **注入点**：调用方自带 `react_ctx` 就用它（离线探针要读那本账——生产与单测都
        # 不传）。自带就意味着**连 `ledger`/`executor` 一起自带**：这两件是"一次运行一个
        # 实例"的非序列化物件，在这儿另建一份只会让调用方手上那本账永远是空的（那正是
        # `eval/experiments/react_line_ab.py` 读台账的方式）。
        if _CTX_KEY not in conf:
            conf[_CTX_KEY] = make_ctx(config=config, principal=principal, role=role,
                                      user_msg=user_msg, state=st,
                                      real_tools=_real_tools(config))
        cfg = {**(config or {}), "configurable": conf,
               "recursion_limit": (config or {}).get("recursion_limit") or REC_LIMIT}
        yield from self._graph.stream(st, cfg,
                                      stream_mode=stream_mode or ["messages", "updates"])


# ── 建臂 ─────────────────────────────────────────────────────────────
def make_ctx(*, config: RunnableConfig | None, principal: Any, role: str | None,
             user_msg: str, state: dict, real_tools: dict | None = None) -> dict:
    """一次运行的 `react_ctx`（**唯一构造点**：`ReactGoldenArm.stream` 与离线探针共用）。

    为什么不放进图 state：图是 `build()` 时编一次、跨请求共用的单例，而账是**一次运行
    一个实例**。放进 state 才是错的（并发两个请求会往同一本账上写）；放进
    `config["configurable"]` 与生产把 `stop_event` / `principal` / `open_tasks` 放那儿
    同源（`server.py` 的 `_run_agent_stream_to_queue`）。

    下面几格是**逐轮变化**的运行状态，缺一不可（每一格都有消费方）：
      · `system_prompt` —— 每请求渲染一次（见模块头注的口径偏差）；
      · `draft` —— 决策模型那条零工具消息，narrator 失败时兜底；
      · `wrap_up` —— 确定性收尾正文（收敛判据/崩溃），`model` 节点照发；
      · `correction` / `corrected` —— 意图纠偏（只此一次）；
      · `decided` —— 路由函数唯一的判据来源；
      · `lines` —— 回执行（只给模型看，见 `_expand`）；
      · `notes` —— 零工具技能的结果（只进窗口，见 `_window`）；
      · `calls` / `origin` —— 决策模型**自己发过的那几条调用**（技能 + 真参数）与
        "每条工具帧是哪一条调用发的"（见 `_pair_frames`）。
    """
    ledger = RunLedger()
    # `page_ctx` 在**这里渲染一次**、由 ctx 分发（20261019）：它此前只在 `planner_node`
    # 里现算，而 `WorkflowExecutor` 也要它（屏幕文案创作的第二个入参）⇒ 两处各算一次
    # 就是两处取值。它与 `user_msg` 同寿命（整个请求内不变：它读的是输入里那条
    # `[System: …]` 消息，后续轮次只往 messages 尾部追加），所以放在这里不引入"逐轮
    # 冻结"那种错。
    page_ctx = _page_ctx(list(state.get("messages") or []), role)
    executor = WorkflowExecutor(real_tools if real_tools is not None else {}, ledger,
                             role=role, principal=principal, user_msg=user_msg,
                             grant=state.get("confirm_grant"), state=state,
                             page_ctx=page_ctx, display_text=display_text)
    return {
        "ledger": ledger, "executor": executor,
        "principal": principal, "role": role, "user_msg": user_msg,
        "page_ctx": page_ctx,
        "config": config,
        "system_prompt": None, "draft": None, "wrap_up": "",
        "correction": "", "corrected": False, "decided": "model",
        "lines": {}, "notes": [], "llm": None,
        "calls": [], "origin": {},
        # 本轮**确定性登记**的任务帧（`graph.planner_node` 那个 `sink` 的同款）。住在这里
        # 而不是在 `planner_node` 里现建：本臂的"重决策一次"是**再进一次本节点**（出口
        # 返回 `{}`），现建的 sink 会在两次调用之间丢掉第一次登记的那几帧——详见
        # `planner_node` 的 docstring。
        "task_sink": [],
    }


def build() -> ReactGoldenArm:
    """`golden_arm.build_agent()` 的入口（约定名，改名要连着改那边）。"""
    return ReactGoldenArm(_compiled())


def _record_llm(node: str, resp: Any, t0: float, **extra: Any) -> None:
    """一次 LLM 调用的用量落 trace——生产那四处 `llm_done` 的同款记账。

    **为什么这条臂必须自己记**（20261019）：用量只在**调用点**取得到
    （`usage_fields(resp)` 读的是响应对象本身），而本臂的调用点是**另一处代码**
    ⇒ 生产那些记账一行都盖不到这里。全量实测：graph 每跑记到约 508 次调用，
    react 只记到 **1** 次（那是 `execute` 里 `_create_display_text` 自己记的，
    它是生产函数、记账在它内部）。而成本与**前缀缓存命中率**全靠这一格
    ⇒ 两臂对照里"成本"那半边此前一直**读不出来**，不是"没有成本"。缺席 ≠ 0
    （同 `usage_fields` 自己的字段契约）。

    字段与生产逐字对齐：`duration_s` ＋ `usage_fields(resp)`（`input` / `output` /
    `cache_read`）。`engine` 由调用方给——planner 那一路传 `native`，与生产
    `planner.llm_done` 同一档，`eval/token_cost_report.py` 的聚合表才按同一行相加。
    """
    record(node, "llm_done", duration_s=round(time.monotonic() - t0, 2),
           **usage_fields(resp), **extra)


def _llm() -> Any:
    """决策用 LLM：**与生产 planner 同一组解码参数**。

    `GOLDEN_ARM` 的两臂必须同解码，否则比的是解码参数而不是循环结构——20261004 那次
    "17× 成本 / 33% 撞上限"的假读数就是这么来的（离线 A/B 头注记着：裸 `get_llm()` 是
    thinking=True + temp 0.7 + 8192 预算）。生产 planner 的**四个**值走 settings
    （`.env` 里 `PLANNER_NATIVE_THINKING=false`），这里照读同一组，不另写常数。

    ⚠️ **温度这一格 20261019 才补上**：此前这里硬写一个常数，而生产读的是
    `settings.planner_temperature` ⇒ 上面那句"同一组解码参数"当时是**假的**，两臂比的
    一直是解码而不是结构。四个旋钮一起锁在 `tests/test_react_decoding_parity.py`。
    """
    return get_llm(temperature=settings.planner_temperature,
                   max_tokens=settings.planner_native_max_tokens,
                   timeout=settings.planner_native_timeout,
                   enable_thinking=settings.planner_native_thinking)


def _narrator_llm() -> Any:
    """narrator 用 LLM：**与生产 `model_node` 同一句**（`get_llm(enable_thinking=False)`）。

    与 `_llm()`（决策那份，走 `planner_native_*` 三个 settings）**故意不共用**：生产就是
    两个不同的参数档（决策要低温度短输出，叙述要能长篇）。共用一个工厂就等于把 P6 刚拆开的
    两件事又捏回一个模型——那时"闸门判的对象与生产同不同种"这个问题会重新变成"看运气"。

    `enable_thinking=False` 的理由与生产那条注同源（长上下文下思考链爆炸，20260830 超时
    事故），本线的 narrator 上下文比生产更长：整份技能表 + 循环说明 + 工具帧。
    """
    return get_llm(enable_thinking=False)


def _narrator_prompt(state: AgentState, receipts: list, drop: set[str],
                     ctx: dict) -> tuple:
    """narrator 的**两条**系统消息 `(稳定头, 变化段)`——生产 `graph._narrator_system_pair`
    本身，本函数只做"哪几个格填什么"（20261019，资产对齐 H）。

    三个格的口径与 `graph.model_node` 逐条对齐，凡是与它不同的地方都是**明写的口径差**：

      · `plan` 走 `_narrator_plan(st, config)`：传的是**真 state**（归一过计划两态，
        见 `_norm_state`），它读的 `plan`/`receipts`/`noop_specs`/`blocked`/
        `blocked_seen`/`pending_confirm`/`ledger`/`confirm_grant` 在这一份里全是真的
        ——**与闸门读的是同一份**；
      · `tool_frames` 走 `article_pointer=True`（生产那一份函数自己就这么调）；
      · `fact_block` 按 `is_action_family` 分岔，判据是**回执**不是"印了几行"（见
        `_NO_PRINTED_FACTS` 的注：20261002 那次"系统发的空授权"就是按错判据分岔的现场）。

    ⚠️ **返回的是两条而不是一条**（20261016 前缀缓存）：稳定头（人设 + 24 条叙述纪律）
    与变化段（计划 / 工具帧 / 回执 / 事实块 / 页面上下文）必须分开装，且**变化段要排在
    对话历史之后**——否则从 `{plan}` 起每轮都不同的那一段会把它后面的一切（包括整整
    20 条历史）一起挤出公共前缀。装配顺序由 `_narrate` 定，两处切分都在
    `react_parity.narrator_system_pair` / `narrator_wire`（借生产那两件，不抄第二份）。
    """
    st = _norm_state(state)
    return narrator_system_pair(
        st, ctx.get("config"), ctx.get("role"), receipts,
        block_of(receipts),
        drop,
        any(is_action_family(r) for r in receipts))


def _narrator_call(msgs: list) -> Any:
    """零工具调用一次，把生产 narrator 那两段补救**照原样**带上。

    两段都是真事故换来的，所以这里一条不少地复用同一套判据与同一份措辞资产：
      · **空内容重试一次**（20261001）：上游偶发"有 completion token、正文却是空串"
        （133 个 narrator 轮里 3 条），重试是"再问一次模型"，比放宽判据更接近事实；
      · **回了 `tool_calls` 的纠正**（20261006）：零工具节点最坏的形态不是报错，而是
        "只写了一句开场白 + 一条 tool_call"——正文非空 ⇒ 闸门判 PASS ⇒ 主人收到一句
        内容为零的话却当成功交付。`_NARRATOR_NO_TOOLCALL_NUDGE` 是那一批的资产。

    返回 `None` = 两次都没拿到可用正文，调用方退回决策模型那条草稿。

    **贴纸记号修复就地改 `resp.content`**（不是另建一条 `AIMessage`）：生产 `model_node`
    就是这么做的，而重建会换掉 id——那条 id 已经被 `messages` 通道认领过（`on_llm_end`
    的 dedupe 靠它），重建等于同一段正文发两遍。
    """
    llm = _narrator_llm()
    _t0 = time.monotonic()
    _chars = sum(len(_msg_text(m) or "") for m in msgs)
    resp = llm.invoke(msgs)
    # 记账（见 `_record_llm`）：节点名与事件名照生产 `model_node`，两条补救路径
    # 也照它的 `model.llm_empty_retry` / `model.llm_toolcall_correct_failed`
    # ——重试那两次也是真花了钱的调用，漏记等于把这一族的成本少算一截。
    _slow = time.monotonic() - _t0 > 30
    (logger.warning if _slow else logger.info)(
        "[react_arm] narrator LLM %s 耗时=%.1fs", "慢调用" if _slow else "完成",
        time.monotonic() - _t0)
    _record_llm("model", resp, _t0, prompt_chars=_chars,
                **({"slow": True} if _slow else {}))
    if not str(getattr(resp, "content", "") or "").strip():
        logger.warning("[react_arm] narrator 返回空内容 → 重试一次")
        resp = llm.invoke(msgs)
        record("model", "llm_empty_retry", **usage_fields(resp))
    if getattr(resp, "tool_calls", None):
        _bad = resp
        logger.warning("[react_arm] narrator 回了 tool_calls（零工具节点不该有）n=%d"
                       " → 带纠正重问一次", len(_bad.tool_calls or []))
        _corr = llm.invoke(msgs + [SystemMessage(content=_NARRATOR_NO_TOOLCALL_NUDGE)])
        record("model", "llm_toolcall_correct_failed", **usage_fields(_corr))
        resp = (_corr if not getattr(_corr, "tool_calls", None)
                and str(getattr(_corr, "content", "") or "").strip() else _bad)
    text = repair_sticker_tokens(str(getattr(resp, "content", "") or ""))
    if not text.strip():
        return None
    resp.content = text
    return resp


def _workflow_menu(role: str | None, deny: set[str] | None = None) -> list[StructuredTool]:
    """技能级菜单：与 planner 看到的 schema **逐字同一份**（`build_tool_schema`）。

    `func` 只是占位——`WorkflowExecutor` 才是真正被调的那一层（它转给真工具）。占位体故意
    抛异常：万一将来接线断了，症状是**响亮的一次调用失败**，而不是"调了个什么都不做的桩"
    （后者会让整轮看起来跑通了、其实一个工具都没执行）。
    """
    out: list[StructuredTool] = []
    # 三个任务伪函数**常绑**（20261015 撤掉 `AGENT_TASK_STATE` 档位，理由见
    # `graph._ledger_next` 的注）。第二条臂与主路径在这里必须同源，否则两条臂看到的
    # 菜单不一样、A/B 读出的差异是菜单差而不是策略差。
    # `deny` 是"这一轮不许再选的技能"（来源与语义见 `react_parity.denied`）：它**不扩权**
    # （只会更小），无阻碍轮传空集 ⇒ schema 逐字节不变。
    for item in build_tool_schema(role, deny=deny or frozenset()):
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


def _real_tools(config: RunnableConfig | None = None) -> dict[str, Any]:
    """真工具名 → 可调用体。**必须是纯函数签名（`**kwargs`）**：

    `WorkflowExecutor` 是 `fn(**args)` 调的，而 `BaseTool.__call__` 的第一个位置参数是
    `tool_input` ⇒ 直接塞 `StructuredTool` 会把它当成"参数名恰好叫 path 的 tool_input"，
    症状是**被包装层兜成错误帧**（不炸图），看起来像"每个工具都 BLOCK error_frame"
    ——判据在开火，其实是适配器写错了。这里统一走 `.invoke(单个 dict)`，与生产
    `graph.execute_node` 的调用形态一致。

    ⚠️ **`config` 必须透传**（20261005 修）：生产方式（`server.py`）把
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


def _system_prompt(state: AgentState, ctx: dict) -> str:
    """系统提示 = 判断器提示词（生产的唯一渲染入口）+ 人设 + **叙述纪律** + 循环说明。

    **为什么拼这几块**：本线的决策模型（`planner` 节点）还兼任"决定够了没有"，而叙述
    纪律那几块是给**说人话**的那一半用的（narrator 节点的提示词是另一份资产）。
    · 第一块就是 planner 逐字那份（`_render_planner_prompt`，"唯一入口"）——技能表、
      规则、输出契约全在里面，**照着抄第二份就是抄一个漂移源**；
    · 第二块是 `BLOG_ASSISTANT_PROMPT`（人设 + 叙述边界 + 诚实底线，narrator 用的同一份
      资产）。没有它，收尾正文是"规划器口吻"的、且没有任何反幻觉纪律；
    · 第三块是 `NARRATOR_DISCIPLINE`（20261005 起，**与生产 narrator 逐字同一份**，
      `graph._EXECUTOR_PROMPT` 的纪律段）。此前一条都没接，代价实测可数：`own_*` 族
      整族慢性红——模型如实说了"读不到"，却**又多派了一句"你先去登录"**，而纪律 20
      明令禁止（生产的账不许算在主人头上）。同族还有"读不到 ≠ 空"那几条；
    · 第四块是 `STICKER_GUIDE`（情绪贴纸的**唯一**名字表）。不接它，`:害羞:` 这些
      记号结构上产不出来（`sticker_*` 用例整族恒红），而语料只认这 12 个名字；
    · 第五块是循环机制（怎么终止），只有三五句。

    **纪律块要带立场改写**（`_DISCIPLINE_STANCE`）：那份纪律是 narrator（零工具节点）
    的立场——第 1 条写着"你没有任何可以直接调用的工具"。本循环正好相反，所以紧挨着它
    前面放一段"哪几句按本循环读、其余一个字不放宽"。**不改写就是自相矛盾的提示词**，
    而矛盾提示词的失效方式是不定向的。

    **入参从"调用方算好的一把散值"改成 `(state, ctx)`**（20261019，资产对齐 B）：这一份
    提示词有十四格注入值，散着传就是"每加一格要记得两边都加"；而**真正的病**是此前
    有几格传的是**空值**（`intent_hints=[]`、`tool_results=空`、`ref_hints=[]`、
    `blocked_rows`/`deny` 干脆没传）——空值不报错，只让模型在一份"这轮什么都没有"的
    提问下决策。现在取值全部收进 `react_parity.planner_prompt_args`（逐格对应表在那边），
    本函数只负责把渲染结果与那几块叙述资产拼起来。

    调用方（`_prompt_of`）**每轮重渲染**，不再按请求缓存：`intent_hints` 读
    `state["executed"]`、`tool_results` 读本轮帧、`ref_hints` 读 `state["tool_data"]`
    ——这三格都**逐轮在变**，缓存住等于第二轮的提示词在说"这一轮还没执行过任何工具"。
    """
    messages = list(state.get("messages") or [])
    role, principal = ctx.get("role"), ctx.get("principal")
    config, user_msg = ctx.get("config"), ctx.get("user_msg") or ""
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
        # 行为**逐字相同**（回复正文一字不差、确认卡载荷的 workflow/tool/args 全同）
        # ——红的是证据通道，不是行为。这与 `forbid_fallback` 那 138 条是同一个病的
        # 两个方向：那边是"做不到 ⇒ 纯否定断言恒真"（假绿），这边是"不产证据 ⇒
        # 要求型断言恒假"（假红）。
        #
        # 条件与生产**逐字同源**（`graph.py` 的 `if rounds == 0 and ledger_frame`）：
        # 空帧不记——"没摆上桌"与"摆了张空的"在那三条判据里是两件事
        # （`require_ledger_frame` 明写「只判摆没摆，不管队列空不空」，而生产连空帧
        # 都不记 ⇒ 本线照抄，别自作聪明放宽）。
        #
        # ⚠️ `rounds == 0` 那一半是 20261019 才补上的：提示词此前**每请求只建一次**
        # （`_prompt_of` 缓存），等价于恒在首轮；改成逐轮重渲染之后，不加这一条就会
        # **每轮记一条**，而 `require_ledger_frame` 的读法是"这一轮有没有摆台账"
        # ⇒ 判据从"摆没摆"漂成"摆了几次"，且方向是恒真。
        if int(state.get("plan_rounds") or 0) == 0:
            record("planner", "ledger_frame", **_meta)
    # 注入值一次算好、一次渲染（`_render_planner_prompt` 是**唯一**渲染点，它只负责
    # 拼字符串；逐格对应与理由见 `react_parity.planner_prompt_args`）。
    _args = planner_prompt_args(
        state, role=role, page_ctx=ctx["page_ctx"], round_info=_ROUND_INFO,
        user_msg=user_msg, ledger_frame=ledger_frame,
        correction=str(ctx.get("correction") or ""), deny=denied(state),
        frames_txt=_frame_texts(_turn_tail(state)))
    return "\n\n".join([_render_planner_prompt(**_args), BLOG_ASSISTANT_PROMPT or "",
                        audience_block(role), _DISCIPLINE_STANCE, NARRATOR_DISCIPLINE,
                        STICKER_GUIDE, _CLOSING_RULES])


def _crash_text(ledger: RunLedger, err: Exception) -> str:
    """崩溃收尾的确定性正文（只说系统确认过的事实，不声称任何动作）。

    **不做**在图外面接异常再手搓帧的那一套——那等于把刚删掉的适配器请回来。异常在
    `planner` / `execute` 节点里就地变成这一句，走 `model` 节点那条确定性正文出口。
    """
    done = [str(r.get("tool") or "") for r in ledger.receipts]
    if done:
        return (f"我停在这里了：内部出了点问题（{type(err).__name__}），这一轮没法继续。"
                f"此前确实办成 {len(done)} 件（{'、'.join(done)}），剩下的你再说一声。")
    return (f"我停在这里了：内部出了点问题（{type(err).__name__}），"
            "这一轮没有任何一件办成，我没有假装办好了。")


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
· **纪律 23 作废**（它按「本轮系统代印的事实」那一格分岔，本循环没有那一格；而 20261005
  起生产那一格也恒为占位、写族同样归 narrator 说）：
  写族的结果**归你说**，按它 ② 那半执行——照回执原话说，不作完成式陈述。
· 纪律里凡说「工具执行记录 / 本轮执行回执」→ 你自己这一轮调用工具后拿到的真实返回
  （就在消息流里）。

其余全部照旧生效：不许编造、**读不到 ≠ 空**、只按回执说结果、不许把还没动手讲成已经
办好、不许给主人派「你先去登录」这类活……**这些一条都不放宽**。"""


# 收尾说明（模块私有，三五句）：把"这个循环里你怎么结束"讲清楚。**不写叙述纪律**
# ——那件事由上面的 `NARRATOR_DISCIPLINE` 负责（纪律前还有 `_DISCIPLINE_STANCE`），
# 这里只讲机制。
_CLOSING_RULES = """\
循环机制（你既是决策者也是回复者）：
1. 要做事就**直接调用技能工具**（上面的技能表就是你的全部本领）；系统会立刻执行并把
   真实结果作为一条工具消息还给你。
2. 信息已经够回答主人了 ⇒ **不要再调用任何工具**——那一轮就到此为止，收尾正文由系统
   另派一位零工具的叙述者按工具回执写（所以你说的每一件站内事实都必须能在工具结果、
   页面上下文或执行回执里找到出处；找不到就如实说没查到）。
3. 调用 `chat` 技能是**闲聊兜底**（把答复文本填进 reply），它不产生任何执行事实；能用
   一句正常的最终答复说清的事，就别绕它。
4. 同一件事不要重复做：工具回执里写着"未生效"就换参数或换工具，别原样重试。
5. 复核：说出口的每一件站内事实，都要能在上面的工具结果、页面上下文或执行回执里找到
   出处；找不到就如实说没查到。"""


# ── plan_obj / blocked 行 ────────────────────────────────────────────
def _chat_plan(role: str | None) -> dict:
    """**纯伪函数轮**（只有 `task_hold`/`task_drop`/`task_intents`）的技能位：`chat`。

    取值与生产逐字同源：`planner_llm.tool_calls_to_plan` 在"摘掉伪函数之后一条真调用
    都不剩"时给的就是 `PlannerDecision(workflow="chat", params={})`，再经
    `_decide_instantiate` → `instantiate_plan("chat", {})`。所以这一格的形状是
    "**模型点过函数、但这一轮没有动作要做**"——不是空计划（空 dict 会让下游读 `workflow`
    的那几处拿到空串，而生产这一轮恒是 `chat`，`_plan_for_gate` 的补缺也就不会触发）。

    `calls` 显式给空列表：它是本臂"这一轮做什么"的唯一载体，缺键与空表在
    `_sync_calls` 里走的是同一条路，但**显式**写出来才读得出"这里确实没有清单"。
    """
    plan = dict(instantiate_plan("chat", {}, role))
    plan["calls"] = []
    return plan


def _plan_of(tool_calls: list, role: str | None) -> dict:
    """模型这一轮点的技能调用 → 生产的 `plan_obj`（producer 只读 `workflow` 与 `tools`）。

    一轮多条调用时合并成一份：`workflow` 用 `|` 连起来，`tools` 取并集——生产
    `parallel_tool_calls=False` 保证它那边恒为一条，这里是有意的容差（见模块头注的
    口径偏差）。

    **`calls` 是本图自己的键**（执行节点的清单来源）：`plan_obj` 是唯一能把"这一轮决定
    做什么"从 `planner` 交到 `execute` 的载体，而**不能**靠消息表（见 `planner_node`
    的 docstring）。`plan_encode` 只读它认得的键，多一个 `calls` 不影响文本态。
    """
    plans: list[dict] = []
    for c in tool_calls:
        name = str((c or {}).get("name") or "")
        args = dict((c or {}).get("args") or {})
        try:
            plans.append(dict(instantiate_plan(name, args, role)))
        except Exception:  # noqa: BLE001 —— 实例化失败不该让整轮消失
            logger.warning("[react_arm] instantiate_plan 失败：workflow=%s args=%s", name, args,
                           exc_info=True)
            plans.append({"workflow": name, "tools": [], "note": "", "reply": "",
                          "status": ""})
    calls = [{"workflow": str((c or {}).get("name") or ""),
              "params": dict((c or {}).get("args") or {}),
              # 实例化出来的清单（**逐字**）。execute 拿到 `specs` 就走 `run_specs`
              # （清单已定稿），拿不到才重跑 `instantiate_plan`——而重跑会**丢掉
              # planner 阶段就地做过的改写**（写目标拒绝链的三条校正、字面路径修正、
              # 去重裁剪都改 `plan_obj["tools"]`）。所以清单在这里落一次，planner
              # 收尾时再由 `_sync_calls` 同步成定稿版。
              "specs": [str(t) for t in (p.get("tools") or [])]}
             for c, p in zip(tool_calls, plans)]
    if not plans:
        return {}
    if len(plans) == 1:
        out = plans[0]
        # 这一行抄的是生产 `_decide_instantiate` 紧随 `instantiate_plan` 的那一句
        # （`plan_obj["params"] = c.params`），**它不是可选的**：`_finalize_dedupe` 的
        # 字面路径修正直接下标这一格，而 `instantiate_plan` 的产物里没有它 ⇒
        # 「navigate 技能 + 原话里带 / 路径」的用例整轮打成 `__ERROR__`（nav_nonexistent
        # 全量实测 3/3：空回复、error='params'）。生产 20260922 在收尾路径上踩过同一个
        # 坑（见 `_decide_write_guard` 那处 refusal 为什么必须 return），治法就是这一行。
        out["params"] = dict(calls[0]["params"])
    else:
        out = {"workflow": "|".join(str(p.get("workflow") or "") for p in plans),
               "tools": [t for p in plans for t in (p.get("tools") or [])],
               "note": "", "reply": "", "status": ""}
        # 多调用那一支是本臂的容差（生产 `parallel_tool_calls=False` 恒一条）⇒ 没有
        # "这一份计划的参数"可言。给空表、不拿第一条的顶替：宁可"没有"显式，也不编一个
        # 看起来像真的（该分支的 `workflow` 是 `a|b` 形状，字面路径修正不会命中）。
        out["params"] = {}
    out["calls"] = calls
    return out
