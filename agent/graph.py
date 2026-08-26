"""手写 LangGraph 图 —— Agent 核心重写（升级阶段 1，课1-4 完成）

替代 create_agent 黑盒：显式声明 planner / model / tools / reflector 节点与状态流转。
面试核心：能讲清楚"你的 agent 循环怎么设计"——
  1. 状态 schema 为什么这么设计（工作台上放什么数据）
  2. 节点分工（每个工人干什么）
  3. 条件边（什么情况下循环、终止、纠错）

架构（课3 定型）——三层混合：
  * 顶层 Plan-and-Replan：planner 拆解计划 → 执行 → reflector 对照检查，
    未通过时修正计划再执行（REVISE 循环，预算 2 次）
  * 执行层 ReAct：model 节点带工具思考 → 有 tool_calls 走 tools → 回到 model，
    直到不再调工具（循环在图上可见，trace 可数"第几步调了几次工具"）
  * 反思层 Reflexion：reflector 对 tool/multi 意图做 LLM 质检；
    chat 意图走快道（非空检查，不花 LLM 钱）

LangGraph 四件套（对照第一课讲解）：
  State  —— AgentState（节点间共享的字典，字段决定"工作台长什么样"）
  Node   —— planner/model/tools/reflector（每个是普通函数：state 进、更新字段出）
  Edge   —— 普通边（顺序传送带）+ 条件边（按返回值路由，循环/终止/纠错所在）
  Reducer—— Annotated[list, add_messages]：messages 字段"追加"而非覆盖

与现有工程外壳的关系（全部保留不动，课4 接入 server.py）：
  _build_messages（历史/摘要注入）、SSE 帧协议、超时体系、recursion_limit、
  force_display 强制路由 —— 都在 server.py，本文件只负责"图长什么样"。
"""

from __future__ import annotations

import json
import logging
import re
from typing import Annotated, Literal, TypedDict

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langgraph.graph import StateGraph, START, END
from langgraph.graph.message import add_messages

from models import get_llm
from tools import get_all_tools
from agent.prompts import BLOG_ASSISTANT_PROMPT
from agent.skills import SKILL_MAP, build_planner_context, instantiate_plan

logger = logging.getLogger(__name__)

# 工具一次构建全局复用（tools/base.py 的 @tool 都是纯函数，无状态）
_TOOLS = get_all_tools()
_TOOL_MAP = {t.name: t for t in _TOOLS}

# reflector 纠错预算：最多 REVISE 2 次，防止反思循环烧钱/烧时间
MAX_REFLECTIONS = 2


# ---------------------------------------------------------------------------
# 1. State：节点间共享的"工作台"
# ---------------------------------------------------------------------------

class AgentState(TypedDict):
    """图状态。planner 写计划，executor 执行并写结果，reflector 检查并写反思。

    - messages:   对话消息（模型/工具往返的流水）。Reducer=add_messages 表示
                  "追加"——这正是 create_agent 里消息只增不减的机制，我们显式声明出来。
    - plan:       planner 拆解出的执行计划（契约文本，见 parse_plan/plan_encode）。
    - reflection: reflector 最近的反思结论："上一步结果够了吗？需要修正什么？"
    - reflection_count: 已反思次数（预算 MAX_REFLECTIONS，防止死循环）。
    - done:       reflector 判定"全部完成"后置 True → 条件边路由到 END。
    """

    messages: Annotated[list, add_messages]
    plan: str
    reflection: str
    reflection_count: int
    done: bool


# ---------------------------------------------------------------------------
# 2. 模块间契约：planner 写入 plan 字段，executor/reflector 读取
# ---------------------------------------------------------------------------
# plan 字段 = 技能模板实例化后的计划文本（受限规划——planner 只从技能注册表
# agent/skills.py 选技能 + 填参数，不自由写步骤）：
#   第 1 行: SKILL=<技能名>（navigate/effect/darkmode/device_display/
#            device_query/content_query/chat）
#   第 2 行: PARAMS=<JSON 参数>（如 {"target": "物联网平台", "mode": "direct"}）
#   第 3 行: TOOLS: <实例化后的工具调用序列>（chat/content_query 等无工具时为"（无）"）
#   第 4 行: NOTE: <业务注记>（如导航目标下线 → 不调用工具、如实告知）
#   第 5 行: REPLY: <技能回复契约>（model 生成时遵守、reflector 对照检查）
# 导航映射表在 skills.py（页面别名→路径，"物联网平台→/device-console/"是系统数据，
# 不是模型猜测）——planner 跑题的结构性根因（不知道工具语义）由此消除。
# 先定格式再写实现——多模块系统（planner/executor/reflector 分工）解耦靠契约。

_PLANNER_PROMPT = """\
你是一个技能选择器。根据用户消息从技能注册表中选择最合适的技能，并给出参数。

{skills_context}

当前可调用的工具（技能执行步骤里的工具名必须与之一致）：
{tools_desc}

规则：
1. 只能从技能表中选择一个技能，不得自创步骤或自由编写执行计划。
2. 闲聊、问候、纯文字问答 → chat 技能。
3. 导航目标在映射表中标记为"已下线"（如友链）时：选 chat 技能如实告知，不要选 navigate。
4. PARAMS 必须严格按技能定义的参数名输出。
5. 访客给出以 / 开头的具体路径时，target 原样填该路径，不要推断它对应哪个页面
   （如 /iot 就是 /iot；路径是否有效由系统按白名单预校验，页面别名才走映射表）。
6. 用户消息含常见导航动词（去/回/到/打开/跳转/访问/进入/返回/转到）且提到页面
   别名或路径时 → navigate；口语化措辞（如"回首页""去留言板"）同样是导航意图，
   不要退化成 chat。仅提及页面但不要求前往（如"首页的文章好看吗"）不选 navigate。

输出严格按以下格式，不要输出任何其他内容：
SKILL: <技能名>
PARAMS: <JSON>

用户消息：{user_msg}"""


def _tools_desc() -> str:
    """从工具注册表动态生成能力清单（单一事实来源，不手写两遍）。

    planner 需要完整描述（含参数语义），不再 80 字符截断——信息不足正是
    旧版 planner 把"去物联网平台"规划成"查设备"的根因之一。
    """
    return "\n".join(
        f"- {t.name}: {t.description}" if t.description else f"- {t.name}"
        for t in _TOOLS
    )


_PLANNER_OUTPUT_RE = re.compile(r"SKILL\s*[:=]\s*(\w+)", re.IGNORECASE)


def _loads_tolerant(text: str):
    """JSON 容错解析：常见漂移（单引号、尾逗号、行注释）逐个修正后重试。

    解析失败返回 None（调用方决定兜底），不抛异常。
    """
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        fixed = re.sub(r"//.*$", "", text, flags=re.M)
        fixed = fixed.replace("'", '"')
        fixed = re.sub(r",\s*([}\]])", r"\1", fixed)
        try:
            return json.loads(fixed)
        except json.JSONDecodeError:
            return None


def _parse_params(raw: str) -> dict:
    """从 planner 输出提取 PARAMS JSON。容错：去 markdown 围栏、取第一个 {...} 块。"""
    m = re.search(r"PARAMS\s*[:=]\s*(\{.*\})", raw, re.IGNORECASE | re.DOTALL)
    if not m:
        return {}
    obj = _loads_tolerant(m.group(1).strip().strip("`"))
    return obj if isinstance(obj, dict) else {}


def plan_encode(plan_obj: dict) -> str:
    """结构化计划（instantiate_plan 产物）→ plan 字段（契约的写端）。"""
    tools = "（无）" if not plan_obj.get("tools") else "; ".join(plan_obj["tools"])
    return (
        f"SKILL={plan_obj['skill']}\n"
        f"PARAMS={json.dumps(plan_obj.get('params', {}), ensure_ascii=False)}\n"
        f"TOOLS: {tools}\n"
        f"NOTE: {plan_obj.get('note') or '（无）'}\n"
        f"REPLY: {plan_obj['reply']}"
    )


def parse_plan(raw: str) -> dict:
    """解析 plan 字段（契约的读端）。容错：解析失败 → 按 chat 兜底（宁可少干活，不硬猜）。

    返回 {"skill", "params", "tools", "note", "reply", "chat"}。
    面试点：所有"LLM 输出 → 程序消费"的边界都要容错解析——LLM 不是 JSON 解析器，
    输出格式漂移是常态，解析器必须能优雅降级。
    """
    m = re.search(r"SKILL\s*[:=]\s*(\w+)", raw or "", re.IGNORECASE)
    skill = m.group(1) if m else "chat"
    params = {}
    pm = re.search(r"PARAMS\s*[:=]\s*(\{.*?\})\s*\n", raw or "", re.IGNORECASE | re.DOTALL)
    if pm:
        obj = _loads_tolerant(pm.group(1).strip().strip("`"))
        if isinstance(obj, dict):
            params = obj
    tools = []
    tm = re.search(r"TOOLS\s*[:=]\s*(.+)", raw or "", re.IGNORECASE)
    if tm:
        tools = [s.strip() for s in tm.group(1).split(";") if s.strip() and s.strip() != "（无）"]
    nm = re.search(r"NOTE\s*[:=]\s*(.+)", raw or "", re.IGNORECASE)
    note = nm.group(1).strip() if nm else ""
    rm = re.search(r"REPLY\s*[:=]\s*(.+)", raw or "", re.IGNORECASE | re.DOTALL)
    reply = rm.group(1).strip() if rm else ""
    return {
        "skill": skill if skill in SKILL_MAP else "chat",
        "params": params,
        "tools": tools,
        "note": note,
        "reply": reply,
        "chat": (skill in SKILL_MAP and SKILL_MAP[skill].chat) or skill == "chat",
    }


# ---------------------------------------------------------------------------
# 3. Node：planner（课2）/ model（课3）/ tools（课3）/ reflector（课3）
# ---------------------------------------------------------------------------

def planner_node(state: AgentState) -> dict:
    """职责：技能选择器——从技能注册表选技能 + 填参数 → 实例化为计划 → 写入 state.plan。

    与旧版（自由写 STEPS 步骤）的本质区别（面试点）：规划空间受限。模型不写
    "怎么做"（执行步骤是技能模板里的静态数据），只回答"做什么"（选技能）和
    "参数是什么"（填参数）。跑题的根因——planner 不知道工具语义/页面映射——由
    技能表注入（导航映射表"物联网平台→/device-console/"是系统数据）结构性消除。
    代价：每次对话多一次 LLM 调用（约 0.3-0.8s），换来可解释且受限的执行路径。
    """
    last = state["messages"][-1]  # 最后一条是当前用户请求
    content = getattr(last, "content", last)
    if not isinstance(content, str):
        content = str(content)
    user_msg = content[-500:]  # 只看最近一段，防止超长输入稀释分类

    # 快思考模块：低温度（分类不需要创造力）、小 max_tokens、短超时
    llm = get_llm(temperature=0.2, max_tokens=300, timeout=30)
    resp = llm.invoke(_PLANNER_PROMPT.format(
        skills_context=build_planner_context(), tools_desc=_tools_desc(), user_msg=user_msg))
    raw = getattr(resp, "content", str(resp))
    skill_name = re.search(r"SKILL\s*[:=]\s*(\w+)", raw, re.IGNORECASE)
    skill_name = skill_name.group(1) if skill_name else "chat"
    params = _parse_params(raw)
    plan_obj = instantiate_plan(skill_name, params)
    plan_obj["params"] = params
    logger.info("[planner] skill=%s params=%s tools=%s", plan_obj["skill"], params, plan_obj["tools"])

    return {"plan": plan_encode(plan_obj), "reflection": "", "reflection_count": 0, "done": False}


_EXECUTOR_PROMPT = """\
{persona}

[执行计划]
{plan}

执行规则：
1. 按计划执行：TOOLS 行列出的是技能模板的固定工具序列——需要调用就调用
   （工具结果以工具返回为准，不要编造）；TOOLS 为（无）时不需要工具，
   直接回答。
2. 若 NOTE 行说明"不调用任何工具"（如导航目标已下线/页面不存在）：
   按 NOTE 如实告知访客即可，不要强行调用工具。
3. 所有步骤完成后，给出最终回复（遵循 REPLY 行的回复契约）。
4. 如果执行中发现计划不适用（例如工具返回与预期不符），按实际情况处理并
   在回复中说明——计划是参考，事实以工具返回为准。
5. 工具调用失败时（返回以 __ERROR__ 或"无效"开头的错误）：立即按错误信息中
   给出的有效参数重试一次；不得以"页面不存在/没有这个功能"为由放弃——
   先重试，重试仍失败才如实向用户说明。"""


def model_node(state: AgentState) -> dict:
    """ReAct 执行层的"思考"节点：带工具思考 → 产出 tool_calls 或最终回答。

    与 create_agent 的 model node 同源，但计划注入是显式的：
    system prompt = 人设 + 当前执行计划（技能模板实例），模型按模板驱动工具调用。
    """
    llm = get_llm()  # 主模型：对话生成用默认参数（温度 0.7、可流式）
    system = SystemMessage(content=_EXECUTOR_PROMPT.format(
        persona=BLOG_ASSISTANT_PROMPT, plan=state["plan"]))
    resp = llm.bind_tools(_TOOLS).invoke([system] + state["messages"])
    logger.info("[model] tool_calls=%s", [c["name"] for c in resp.tool_calls])
    return {"messages": [resp]}


def tools_node(state: AgentState) -> dict:
    """ReAct 执行层的"行动"节点：执行上一条消息里的所有工具调用。

    手写版 ToolNode（面试点：create_agent 内部就是这个逻辑，我们显式写出来）：
      1. 逐条执行 tool_calls，结果打包成 ToolMessage（tool_call_id 关联回调用）
      2. 按 (name, args) 去重——模型重试时可能重复发同一调用，
         副作用工具（切换特效/导航）执行两次是真 bug
      3. 异常不炸图：错误信息作为工具结果返回，让模型自行理解修正
    """
    last = state["messages"][-1]
    results, seen = [], set()
    for call in last.tool_calls:
        key = (call["name"], json.dumps(call.get("args", {}), sort_keys=True))
        if key in seen:
            continue
        seen.add(key)
        tool = _TOOL_MAP.get(call["name"])
        if tool is None:
            out = f"__ERROR__: 未知工具 {call['name']}"
        else:
            try:
                out = tool.invoke(call.get("args", {}))
            except Exception as e:
                out = f"__ERROR__: {type(e).__name__}: {e}"
        results.append(ToolMessage(content=str(out), tool_call_id=call["id"], name=call["name"]))
        logger.info("[tools] %s → %.100s", call["name"], str(out))
    return {"messages": results}


_REFLECTOR_PROMPT = """\
你是执行质量检查员。对照技能模板检查对话执行轨迹。

技能模板（计划）：
{plan}

执行轨迹（摘要）：
{trace}

检查要点：
1. 计划中 TOOLS 行要求的工具调用是否完成（对应工具已调用且返回成功）？
   轨迹已按轮次裁剪：只含最近一次修正注记之后的最新一轮执行（+用户消息），
   历史被作废轮次不参与判罚。TOOLS 为（无）时，模型未调用工具直接回答即为符合模板。
2. 工具结果是否有 __ERROR__？
3. 最终回答是否基于工具返回的事实（有没有编造）？
4. 是否回答了用户的问题？
5. 若模型因合理原因（功能/页面已下线——见 NOTE 行、数据不存在、工具返回错误后
   重试仍失败、访客明确禁止调用工具/模型无法调用工具）如实告知访客而未能完成
   TOOLS 行的某个调用：视为合理处理，判 PASS。此时如实拒绝并给出页面
   Markdown 链接是合格的最终回答。
   只有模型编造事实、或声称已完成实际未执行的动作时才判 REVISE。
6. 风格问题不判 REVISE：TOOLS 行要求的工具已成功调用（帧已产出）后，
   正文是否附 Markdown 链接、链接格式、措辞风格均不影响判罚——跳转/执行
   由系统帧完成，正文只是确认。
{idem_note}输出严格按以下格式，不要输出其他内容：
VERDICT: PASS 或 REVISE
NOTE: 一句话说明（PASS 写通过理由；REVISE 写具体要修正什么）"""


def _build_trace(messages: list) -> str:
    """把消息流水压成紧凑执行轨迹（给 reflector 看，省 token）。"""
    parts = []
    for m in messages:
        if isinstance(m, AIMessage) and m.tool_calls:
            parts.append(f"助手调用工具: {m.tool_calls[0]['name']}({json.dumps(m.tool_calls[0].get('args', {}), ensure_ascii=False)[:80]})")
        elif isinstance(m, ToolMessage):
            parts.append(f"工具返回: {m.content[:100]}")
        elif isinstance(m, AIMessage) and m.content:
            parts.append(f"助手回答: {m.content[:100]}")
        elif hasattr(m, "content") and isinstance(m.content, str) and m.content:
            parts.append(f"{m.__class__.__name__}: {m.content[:80]}")
    return "\n".join(parts)[-800:]  # 只留最近 800 字符


def _current_round(messages: list) -> list:
    """当前轮消息：最近一次修正注记（SystemMessage）之后的轨迹。

    state 里的消息只有 reflector 追加的修正注记是 SystemMessage（executor
    system prompt 不进 state），故"最后一个 SystemMessage 之后"即当前（最近
    一轮修正后的）轨迹；无修正注记时即整个消息流（首轮）。
    用途：确定性模板检查只查当前轮——历史轮已调工具不能豁免当前轮的缺失
    （曾见：首轮调用成功后被 REVISE，次轮仅输出文本链接未再调工具，全局
    扫描看到历史 ToolMessage 就放行，最终无帧收尾）。
    """
    idxs = [i for i, m in enumerate(messages) if isinstance(m, SystemMessage)]
    start = idxs[-1] + 1 if idxs else 0
    return messages[start:]


def reflector_node(state: AgentState) -> dict:
    """反思层：对照技能模板质检执行结果。

    快慢两条道（面试点：反思也要算成本）：
      - chat 技能：不花 LLM 钱，只做非空检查（闲聊无执行可查，反思是浪费）
      - 其余技能：LLM 对照技能模板（TOOLS/NOTE/REPLY）+ 轨迹出 VERDICT：
          PASS   → done=True，收尾
          REVISE → 追加一条 [Reflection] 修正注记进 messages（紧贴当前轮，
                   遵守率最高），回 model 重来；预算 MAX_REFLECTIONS，耗尽即收
    模板质检天然覆盖旧版程序化闸门要抓的场景（模型假装执行）：TOOLS 行要求的
    工具若在轨迹中缺失，检查点 1 即判 REVISE——执行必须真发生，文本表演过不了
    模板比对。
    """
    plan = parse_plan(state["plan"])
    count = state.get("reflection_count", 0)

    last = state["messages"][-1]
    last_content = (getattr(last, "content", "") or "").strip()

    if plan["chat"]:
        return {"done": bool(last_content), "reflection": "chat 快道路：非空检查通过" if last_content else "chat 回复为空", "reflection_count": count}

    # 设备显示注记快道：后端已按 force_display 强制执行（注记携带执行结果），
    # reply_contract 授权"直接按注记如实回复、不重复调用工具"——零工具调用是合法
    # 契约行为。LLM 质检看不到注记（trace 截断 HumanMessage），会把本轮误判为
    # "TOOLS 未完成"而 REVISE（曾造成回复文本两轮拼接重复）；且执行已由后端保证，
    # 无再查必要——确定性 PASS（同 chat 快道，只做非空检查）。
    _DISPLAY_NOTE_MARK = "系统已按访客要求执行设备屏幕显示"
    has_display_note = any(
        _DISPLAY_NOTE_MARK in (getattr(m, "content", "") or "")
        for m in state["messages"] if isinstance(m, HumanMessage)
    )
    if has_display_note:
        logger.info("[reflector] 设备显示注记快道：后端已执行，PASS")
        return {"done": bool(last_content), "reflection": "设备显示注记快道：后端已执行，无需重复调用工具", "reflection_count": count}

    if count >= MAX_REFLECTIONS:
        # 预算耗尽 → 接受当前结果收尾：纠错循环必须有上限，不无限烧钱/烧时间
        logger.info("[reflector] 反思预算耗尽(%d/%d)，接受当前结果", count, MAX_REFLECTIONS)
        return {"done": True, "reflection": f"反思预算已用尽({count}/{MAX_REFLECTIONS})，接受当前结果", "reflection_count": count}

    # effect/darkmode 幂等判定（确定性计算，注入 LLM 质检上下文）：
    # 按上下文 current_effects/current_darkmode 与计划参数计算"状态是否与目标一致"。
    # 背景：LLM 质检会把回复契约的"与目标一致时不调用工具"条款理解反——曾把
    # current=sakura、目标=off（不一致，必须调工具）误判为"一致、调用违规"，
    # 把正确执行 REVISE 掉（帧随 __RESET__ 作废 → golden 缺帧失败）。程序先算好
    # 事实注入质检上下文，两个方向的误判都消除。
    # 注意：这里只注入事实、不做确定性 REVISE——零工具可能是合理拒绝（如注入攻击
    # 轮），程序无法区分"拒绝"与"偷懒声称完成"，判罚交给 LLM（检查点 5 放行拒绝）。
    state_matches: bool | None = None
    if plan["skill"] in ("effect", "darkmode") and plan["params"]:
        ctx_text = "\n".join((getattr(m, "content", "") or "")
                             for m in state["messages"] if isinstance(m, HumanMessage))
        if plan["skill"] == "effect":
            eff, act = plan["params"].get("effect"), plan["params"].get("action")
            if eff and act:
                m = re.search(r"current_effects=([^\s,;]+)", ctx_text)
                cur = m.group(1) if m else "none"
                state_matches = (act == "on" and eff == cur) or (act == "off" and eff != cur)
        else:
            mode = plan["params"].get("mode")
            if mode:
                m = re.search(r"current_darkmode=([^\s,;]+)", ctx_text)
                cur = m.group(1) if m else "off"
                state_matches = (mode == "on" and cur == "on") or (mode == "off" and cur == "off")

    # 模板执行的结构性检查（检查点 1 的确定性部分，LLM 质检前的低成本闸）：
    # 只对 navigate 生效——导航是"动作必须真发生"的契约（无幂等豁免），TOOLS 行
    # 要求调用 navigate_to 但当前轮从未执行过任何工具 → 正文纯文本声称"已经
    # 跳转"没有依据，直接 REVISE，不花 LLM 钱。检查范围限定当前轮（_current_round）
    # 而非全历史：被 REVISE 的历史轮调用过工具不代表当前轮也调了。
    # 豁免面（合法零调用，不误伤）：NOTE 行下线/未识别/不存在目标 → 计划 TOOLS
    # 已为空且注记明示"不调用任何工具"（下方另有反向检查兜住越权调用）。
    if (plan["tools"]
            and all("navigate_to" in t for t in plan["tools"])
            and not any(isinstance(m, ToolMessage) for m in _current_round(state["messages"]))):
        correction = SystemMessage(content=(
            "[Reflection 检查未通过：计划要求调用 navigate_to 工具，但本轮对话没有任何工具执行。] 修正要求："
            "立即调用计划 TOOLS 行要求的 navigate_to 工具（工具返回后再按结果回复）；"
            "若因访客明确禁止等原因无法调用工具，如实拒绝并给出页面链接即可，不得声称已跳转。"
        ))
        logger.info("[reflector] 模板执行缺失：navigate 计划零工具调用")
        return {"messages": [correction], "done": False,
                "reflection": "navigate 计划要求工具但零工具调用", "reflection_count": count + 1}

    # 反向结构性检查：计划 NOTE 行明示"不调用任何工具"（友链下线/页面不存在等，
    # 此时 TOOLS 为空），但模型仍越权调用了工具（如主动跳转留言板）→ 违反模板
    # 契约，REVISE。此检查只对带该标记的计划生效，不影响 content_query 等自由用
    # 工具的技能。同样限定当前轮（历史轮已作废，不构成越权）。
    if ("不调用任何工具" in plan["note"]
            and any(isinstance(m, ToolMessage) for m in _current_round(state["messages"]))):
        correction = SystemMessage(content=(
            "[Reflection 检查未通过：计划要求不调用任何工具（如实告知即可），但本轮调用了工具。] 修正要求："
            "不要调用任何工具，直接在文本中如实告知（可给出其他页面的 Markdown 链接作为建议）。"
        ))
        logger.info("[reflector] 模板越权：NOTE 要求零工具但调用了工具")
        return {"messages": [correction], "done": False,
                "reflection": "NOTE 要求零工具但调用了工具", "reflection_count": count + 1}

    idem_note = ""
    if state_matches is not None:
        idem_note = (
            f"系统幂等判定：当前状态与目标{'一致' if state_matches else '不一致'}。"
            f"{'一致 → 不调用工具直接答复是符合契约的正确行为；即使调用了工具（工具幂等），也不得判违规' if state_matches else '不一致 → 调用工具是正确行为，不得以“与目标一致”为由判违规'}。\n"
        )
    # 轨迹同样按轮次裁剪（与确定性检查同口径）：被 REVISE 的历史轮不参与判罚，
    # 否则 LLM 会把旧轮的越权调用算到当前轮头上（曾把正确的拒绝轮 REVISE 掉）。
    # 保留最近两条用户消息，质检"是否回答了用户的问题"仍有依据。
    cur = _current_round(state["messages"])
    if cur is state["messages"]:
        trace = _build_trace(state["messages"])
    else:
        humans = [m for m in state["messages"] if isinstance(m, HumanMessage)]
        trace = _build_trace(humans[-2:] + cur)
    # 质检只要 VERDICT 判定，关闭思考：thinking 占满 max_tokens=200 时 content
    # 会被截断成空串（Qwen 思考走 reasoning_content 不进 content），导致
    # PASS 但 reflection 为空、单测错判
    llm = get_llm(temperature=0.0, max_tokens=200, timeout=30, enable_thinking=False)
    resp = llm.invoke(_REFLECTOR_PROMPT.format(plan=state["plan"], trace=trace, idem_note=idem_note))
    raw = getattr(resp, "content", str(resp))
    if re.search(r"VERDICT\s*[:=]\s*REVISE", raw, re.IGNORECASE):
        m = re.search(r"NOTE\s*[:=]\s*(.+)", raw, re.IGNORECASE)
        note = m.group(1).strip() if m else "未按计划执行"
        correction = SystemMessage(content=(
            f"[Reflection 检查未通过：{note}] 修正要求："
            f"1) 若本轮尚未调用任何工具，立即调用计划 TOOLS 行要求的工具，工具返回后再回复，不得直接声称已完成；"
            f"2) 若上次工具调用失败（如路径无效），立即用错误信息中给出的有效参数重试；"
            f"3) 不得向用户声称页面/数据不存在——先重试，重试仍失败才如实说明；"
            f"4) 你之前的回复已作废且不会展示，直接输出修正后的最终回复（简短确认即可，不要重复之前的文字）。"
        ))
        logger.info("[reflector] REVISE: %s", note)
        return {"messages": [correction], "done": False, "reflection": note, "reflection_count": count + 1}

    logger.info("[reflector] PASS")
    return {"done": True, "reflection": raw, "reflection_count": count}


# 4. Edge：条件边 —— 路由逻辑（循环/纠错/终止都在这）
# ---------------------------------------------------------------------------

def route_after_model(state: AgentState) -> Literal["tools", "reflector"]:
    """model 思考完：
      - 有 tool_calls → 去 tools 执行（ReAct 循环继续）
      - 无 → 去 reflector 质检（本步收尾）
    """
    last = state["messages"][-1]
    return "tools" if isinstance(last, AIMessage) and last.tool_calls else "reflector"


def route_after_reflector(state: AgentState) -> Literal["model", END]:
    """reflector 检查完：
      - done=True（通过）→ 结束
      - 未通过 → 回 model 重来（带修正注记）
    """
    return END if state.get("done") else "model"


# ---------------------------------------------------------------------------
# 5. 组装与编译
# ---------------------------------------------------------------------------

def build_graph():
    """构建手写图：节点 + 边 + 编译。返回 CompiledStateGraph。

    拓扑（课3 定型）：
      START → planner → model ─┬─ tool_calls → tools → model（ReAct 循环）
                               └─ 无调用 → reflector ─┬─ PASS → END
                                                     └─ REVISE → model（纠错循环）
    """
    g = StateGraph(AgentState)

    g.add_node("planner", planner_node)
    g.add_node("model", model_node)
    g.add_node("tools", tools_node)
    g.add_node("reflector", reflector_node)

    g.add_edge(START, "planner")
    g.add_edge("planner", "model")
    g.add_conditional_edges("model", route_after_model, {"tools": "tools", "reflector": "reflector"})
    g.add_edge("tools", "model")  # 工具执行完必然回模型看结果（普通边，无需路由）
    g.add_conditional_edges("reflector", route_after_reflector, {"model": "model", END: END})

    return g.compile()


def graph_input(messages: list) -> dict:
    """图输入构造：state 形状归本模块管，调用方（server.py）不手写字段。

    planner 节点会立刻写入 plan/reflection/reflection_count/done，
    这里给空初值只为了让输入形状完整、可读。
    """
    return {"messages": messages, "plan": "", "reflection": "", "reflection_count": 0, "done": False}
