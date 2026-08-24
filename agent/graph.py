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
from functools import lru_cache
from typing import Annotated, Literal, TypedDict

from langchain_core.messages import AIMessage, SystemMessage, ToolMessage
from langgraph.graph import StateGraph, START, END
from langgraph.graph.message import add_messages

from models import get_llm
from tools import get_all_tools
from agent.prompts import BLOG_ASSISTANT_PROMPT

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
# plan 字段 = 编码后的计划文本：
#   第 1 行: INTENT=<chat|tool|multi>
#     chat  —— 闲聊/纯文字问答，不需要工具，model 直接回答
#     tool  —— 需要调用 1 个工具或做 1 次查询
#     multi —— 需要依次做多件事（模型按步骤清单依次执行）
#   后续行: - <步骤描述>（chat 时为空）
# 先定格式再写实现——这是多模块系统（planner/executor/reflector 分工）
# 的核心工程习惯：节点间解耦靠"契约"，不靠互相读代码。

_PLANNER_PROMPT = """\
你是一个任务规划器。根据用户消息判断意图，并给出执行计划。

当前可调用的能力（工具）：
{tools_desc}

规则：
1. INTENT 取值：
   - chat  —— 闲聊、问候、情感交流、纯文字问答（不涉及博客数据或工具）
   - tool  —— 需要调用 1 个工具或做 1 次查询（如导航、特效、夜间模式、查文章、查天气、查设备）
   - multi —— 需要依次做多件事（如"查文章 + 看天气"，或一个需要多步查证的请求）
2. 输出严格按以下格式，不要输出任何其他内容（INTENT=chat 时 STEPS 保持空）：
INTENT: <chat|tool|multi>
STEPS:
- <第1步：做什么、用哪个工具/查什么>
- <第2步>

用户消息：{user_msg}"""


def _tools_desc() -> str:
    """从工具注册表动态生成能力清单（单一事实来源，不手写两遍）。"""
    return "\n".join(
        f"- {t.name}: {t.description.splitlines()[0][:80]}" if t.description else f"- {t.name}"
        for t in _TOOLS
    )


def plan_encode(intent: str, steps: list[str]) -> str:
    """把意图+步骤编码为 plan 字段（契约的写端）。"""
    lines = [f"INTENT={intent}"]
    lines += [f"- {s}" for s in steps]
    return "\n".join(lines)


def parse_plan(raw: str) -> tuple[str, list[str]]:
    """解析 plan 字段（契约的读端）。容错：解析失败 → 按 chat 兜底（宁可少干活，不硬猜）。

    面试点：所有"LLM 输出 → 程序消费"的边界都要容错解析——LLM 不是 JSON 解析器，
    输出格式漂移是常态，解析器必须能优雅降级。
    """
    m = re.search(r"INTENT[:=]\s*(chat|tool|multi)", raw or "", re.IGNORECASE)
    intent = m.group(1) if m else "chat"
    steps = [s.strip().lstrip("- ").strip() for s in (raw or "").splitlines() if s.strip().startswith("-")]
    steps = [s for s in steps if s]
    return intent, steps


# ---------------------------------------------------------------------------
# 3. Node：planner（课2）/ model（课3）/ tools（课3）/ reflector（课3）
# ---------------------------------------------------------------------------

def planner_node(state: AgentState) -> dict:
    """职责：理解用户请求 → 意图分类 + 任务拆解 → 写入 state.plan。

    为什么需要它（面试点）：纯 ReAct 是"边想边做"，模型每轮自己决定下一步；
    planner 先把任务拆清楚再执行——长任务（多步查询/多工具协作）更稳，也便于
    reflector 对照检查"有没有按计划走"。代价是每次对话多一次 LLM 调用
    （约 0.3-0.8s），换来的是可解释的执行路径。
    """
    last = state["messages"][-1]  # 最后一条是当前用户请求
    content = getattr(last, "content", last)
    if not isinstance(content, str):
        content = str(content)
    user_msg = content[-500:]  # 只看最近一段，防止超长输入稀释分类

    # 快思考模块：低温度（分类不需要创造力）、小 max_tokens、短超时
    llm = get_llm(temperature=0.2, max_tokens=300, timeout=30)
    resp = llm.invoke(_PLANNER_PROMPT.format(tools_desc=_tools_desc(), user_msg=user_msg))
    raw = getattr(resp, "content", str(resp))
    intent, steps = parse_plan(raw)
    logger.info("[planner] intent=%s steps=%s", intent, steps)

    return {"plan": plan_encode(intent, steps), "reflection": "", "reflection_count": 0, "done": False}


_EXECUTOR_PROMPT = """\
{persona}

[执行计划]
{plan}

执行规则：
1. 按计划执行：需要调用工具就调用（工具结果以工具返回为准，不要编造）；
   不需要工具就直接回答。
2. 所有步骤完成后，给出最终回复。
3. 如果执行中发现计划不适用（例如计划引用的页面/数据不存在），按实际情况
   处理并在回复中说明——计划是参考，事实以工具返回为准。
4. 工具调用失败时（返回以 __ERROR__ 或"无效"开头的错误）：立即按错误信息中
   给出的有效参数重试一次；不得以"页面不存在/没有这个功能"为由放弃——
   先重试，重试仍失败才如实向用户说明。"""


def model_node(state: AgentState) -> dict:
    """ReAct 执行层的"思考"节点：带工具思考 → 产出 tool_calls 或最终回答。

    与 create_agent 的 model node 同源，但计划注入是显式的：
    system prompt = 人设 + 当前执行计划，模型按计划驱动工具调用。
    """
    llm = get_llm()  # 主模型：对话生成用默认参数（温度 0.7、可流式）
    system = SystemMessage(content=_EXECUTOR_PROMPT.format(persona=BLOG_ASSISTANT_PROMPT, plan=state["plan"]))
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
你是执行质量检查员。对照执行计划检查对话执行轨迹。

执行计划：
{plan}

执行轨迹（摘要）：
{trace}

检查要点：
1. 计划中的每个步骤是否都执行了？（调用过对应工具/给出过回答）
2. 工具结果是否有 __ERROR__？
3. 最终回答是否基于工具返回的事实（有没有编造）？
4. 是否回答了用户的问题？
5. 若模型因合理原因（功能/页面已下线、数据不存在、工具返回错误后重试仍失败、
   访客明确禁止调用工具/模型无法调用工具）如实告知访客而未能完成计划的某个步骤：
   视为合理处理，判 PASS。此时如实拒绝并给出页面 Markdown 链接是合格的最终回答。
   只有模型编造事实、或声称已完成实际未执行的动作时才判 REVISE。
6. 若助手回复正文中出现 AUTO_NAVIGATE:/NAVIGATE:/EFFECT:/DARKMODE: 前缀的命令文本，
   但执行轨迹中对应工具调用缺失（如轨迹有"助手回答: AUTO_NAVIGATE:..."而无
   "助手调用工具: navigate_to"）：视为伪造命令（假装执行），判 REVISE——要求真正调用对应工具。
   注意：命令文本来自工具返回（轨迹中已有对应工具调用）时不视为违规。
7. 若助手回复正文出现"已经到X了/啦""已跳转/已来到X"式完成声称（如"我们已经到留言板啦"），
   但执行轨迹中没有任何工具调用：视为虚构完成（假装执行），判 REVISE。
   注意：轨迹中已有工具调用（如"助手调用工具: navigate_to"）后的确认性复述不视为违规。
8. 若助手回复正文是"现在/这就/马上/立刻…带主人去X""马上就到X了"式将来时承诺
   （口头承诺即将跳转/执行），但执行轨迹中没有任何工具调用：同样视为虚构执行，判 REVISE。
   注意：轨迹中已有工具调用的轮次，其承诺/确认性话语不视为违规。

输出严格按以下格式，不要输出其他内容：
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


# 正文伪造命令检测（第二道闸，程序化、零成本）：
# 模型有时不调工具，直接在回复正文里模仿工具返回的命令格式（AUTO_NAVIGATE:/talk 等），
# 以为"输出文本=执行动作"。reflector 的 LLM 质检是概率判断（要点 6），
# 这里先做确定性检测：AIMessage 正文出现命令前缀、但该轮未调用对应工具 → 直接 REVISE。
_CMD_PREFIX_TOOL = {
    "AUTO_NAVIGATE": "navigate_to",
    "NAVIGATE": "navigate_to",
    "EFFECT": "toggle_effect",
    "DARKMODE": "toggle_dark_mode",
}


def _current_round(messages: list) -> tuple[AIMessage | None, set]:
    """取当前轮：最近一次修正注记（SystemMessage）之后的窗口。

    返回 (最后一条 AIMessage, 本轮调用过的工具名集合)。轮次边界用修正注记切分——
    此前轮次若已被 REVISE 作废，其文本不归罪当前轮（否则修正成功后旧文本会反复
    触发打回，白白烧掉反思预算）；工具调用同理按整轮判定（模型先调工具、后在
    正文复述命令/确认到达，是"真执行"，不能误伤）。
    """
    final_ai: AIMessage | None = None
    called: set = set()
    for m in reversed(messages):
        if isinstance(m, SystemMessage):
            break  # 上一个修正注记 = 轮次边界
        if isinstance(m, AIMessage):
            if final_ai is None:
                final_ai = m
            called |= {c["name"] for c in m.tool_calls}
        elif isinstance(m, ToolMessage):
            called.add("__tool__")  # 工具结果存在即视为本轮执行过动作
    return final_ai, called


def _fake_command_in(messages: list) -> str | None:
    """检查当前轮 AIMessage 正文是否伪造命令前缀（如 'AUTO_NAVIGATE'）；无则 None。

    历史注入（HumanMessage）与工具结果（ToolMessage）天然不参与判定。
    """
    final_ai, called = _current_round(messages)
    if final_ai is None:
        return None
    content = getattr(final_ai, "content", "") or ""
    if not isinstance(content, str):
        return None
    for prefix, tool_name in _CMD_PREFIX_TOOL.items():
        if tool_name in called:
            continue  # 本轮真调了对应工具，正文复述命令格式不算伪造
        # 前导放宽：\b 在 "SNOW_EFFECT" 这类变形前缀（下划线粘连）处不成立，
        # 用 [\W_] 覆盖行首/标点/下划线粘连三种情况
        if re.search(rf"(?:^|[\W_])(?:{prefix})\s*:\s*\S", content, re.IGNORECASE):
            return prefix
    return None


# 第三道闸：声称完成但无动作（Gate2 的盲区）
# 模型被 REVISE 逼到墙角时，可能不再写命令前缀，而是纯文本声称"已经到X了/啦"——
# 没有前缀可查，但同样没有工具调用，跳转并未真实发生（如"我们已经到物联网平台啦"）。
# 目标词收窄到导航页别名，避免"已经到下午三点了"这类时间/状态声称误伤。
_NAV_TARGET_RE = (
    r"(?:物联网|平台|设备控制台|控制台|友链|友情链接|留言板|说说|动态|碎语|"
    r"首页|主页|归档|时间轴|关于我|后台|页面|/talk|/friends|/device-console|"
    r"/times|/about|/dashboard)"
)
_FAKE_CLAIM_RE = re.compile(
    r"(?:已经|已)(?:[为带]?主人|为您)?(?:跳转|转跳|导航|来到|到达|抵达|进入|到)"
    r"[^。！？\n，,；;]{0,20}?" + _NAV_TARGET_RE + r"[^。！？\n，,；;]{0,15}?(?:了|啦|喵|完成|成功)"
)


def _fake_claim_in(messages: list) -> bool:
    """检查当前轮正文是否"声称已完成跳转但实际未执行"。

    命中条件：正文出现"已经(为?主人)?(跳转/来到/到达/进入/到)+<导航目标页>+了/啦"式
    声称，且本轮未调用任何工具。真调了工具（任一，含工具结果帧）→ 声称有执行依据，
    不判（模型先调 navigate_to、再在正文确认"已经为您跳转…"是"真执行"，不能误伤）。
    """
    final_ai, called = _current_round(messages)
    if final_ai is None or called:
        return False
    content = getattr(final_ai, "content", "") or ""
    if not isinstance(content, str):
        return False
    return bool(_FAKE_CLAIM_RE.search(content))


# 将来时承诺（Gate2/Gate3 的盲区）：既不写命令前缀、也不用完成时声称，而是"现在/这就/
# 马上…带主人去X"式承诺——同样没有任何工具调用，跳转不会发生（如实测案例"现在立刻马上用
# 真正的魔法带主人去物联网平台，请主人看着屏幕变化哦~"：无前缀可查、无"已经"完成时，
# 前两道闸全部放过，模型口头承诺却零动作）。承诺词 + 导航动词 + 目标页 = 承诺执行跳转；
# 整轮无工具即判；真调了工具的正常"这就带主人去"轮整轮豁免（复用 _current_round）。
# 防误伤三闸：疑问句（"这就去留言板吗？"是征询而非承诺执行）、否定句（"现在不带主人去"
# 是拒绝）、主宾不分（"主人现在可以去物联网平台了"是陈述访客权限而非助手承诺——剥离
# "带主人/为您"等宾语短语后残留"主人/访客/用户"即跳过）。
_FAKE_PROMISE_RE = re.compile(
    r"(?:这就|马上|立刻|立即|现在|这就马上)(?:[^。！？\n，,；;]{0,12}?)?"
    r"(?:带主人|为主人|给主人|带您|为您|陪主人|领主人|带访客|为访客|给访客|带用户|给用户)?"
    r"(?:转跳|跳转|导航|带|去|到|进入|打开|前往|出发)"
    r"(?:[^。！？\n，,；;]{0,8}?)?" + _NAV_TARGET_RE + r"(?:[^。！？\n，,；;]{0,8})?"
)
_PROMISE_OBJECT_PHRASES = ("带主人", "为主人", "给主人", "带您", "为您", "陪主人", "领主人",
                           "带访客", "为访客", "给访客", "带用户", "给用户")
_PROMISE_NEG_RE = re.compile(r"(?:不|别|没|还没|先不)(?:带|去|到|走|跳转|转跳|导航|前往|进入|打开)")
_PROMISE_QUESTION_RE = re.compile(r"(?:吗|好不好|行不行|行不|怎么样|如何)")


def _fake_promise_in(messages: list) -> bool:
    """检查当前轮正文是否"承诺即将执行跳转但实际不会发生"（将来时幻觉）。

    命中条件：承诺词（现在/这就/马上/立刻/立即）+ 导航动词 + 目标页，且本轮未调用任何
    工具——"这就带主人去物联网平台"却没有工具调用 = 口头承诺，跳转不会发生。整轮豁免、
    疑问/否定/主宾陈述等误伤情形见 _FAKE_PROMISE_RE 注释。
    """
    final_ai, called = _current_round(messages)
    if final_ai is None or called:
        return False
    content = getattr(final_ai, "content", "") or ""
    if not isinstance(content, str):
        return False
    m = _FAKE_PROMISE_RE.search(content)
    if not m:
        return False
    span = m.group(0)
    # 否定句（"现在不带主人去"/"这就别去留言板"）是拒绝不是承诺
    if _PROMISE_NEG_RE.search(span):
        return False
    # 疑问句是征询不是承诺执行（"？"不在捕获组内时靠"吗/好不好"等疑问词兜底）
    if "？" in span or "?" in span or _PROMISE_QUESTION_RE.search(span):
        return False
    # 能力/许可陈述（"现在可以/可以去X了"）是描述可否前往，不是承诺执行——即使
    # 主语"主人"在捕获组之外（如"主人现在可以去物联网平台了"）也能靠此闸拦住
    if "可以" in span:
        return False
    # 剥离宾语短语后仍残留"主人/访客/用户"→ 主宾结构（"主人现在可以去X"陈述访客权限）
    stripped = span
    for ph in _PROMISE_OBJECT_PHRASES:
        stripped = stripped.replace(ph, "")
    if any(w in stripped for w in ("主人", "访客", "用户")):
        return False
    return True


def reflector_node(state: AgentState) -> dict:
    """反思层：对照计划质检执行结果。

    快慢两条道（面试点：反思也要算成本）：
      - chat 意图：不花 LLM 钱，只做非空检查（闲聊无事实可查，反思是浪费）
      - tool/multi 意图：LLM 对照计划+轨迹出 VERDICT：
          PASS   → done=True，收尾
          REVISE → 追加一条 [Reflection] 修正注记进 messages（紧贴当前轮，
                   遵守率最高），回 model 重来；预算 MAX_REFLECTIONS，耗尽即收
    另有三道程序化检测（零成本、与 LLM 质检共用 MAX_REFLECTIONS 预算）：
      - _fake_command_in：正文伪造命令前缀 → 直接 REVISE
      - _fake_claim_in：纯文本声称"已跳转/已到达X"但未调工具 → 直接 REVISE
      - _fake_promise_in：将来时承诺"现在/这就/马上…带主人去X"但未调工具 → 直接 REVISE
    预算耗尽且最终轮仍违规 → Gate4 诚实兜底：不接受谎言收尾，标记 fallback，
    server 层回放本对话最近一个通过质检轮的诚实回复作为最终输出。
    """
    intent, _ = parse_plan(state["plan"])
    count = state.get("reflection_count", 0)

    # 第二道闸：正文出现命令前缀但未调用对应工具 = 假完成，直接打回
    fake = _fake_command_in(state["messages"])
    # 第三道闸：纯文本声称"已跳转/已到达X"但未调用任何工具 = 假完成（无前缀可查）
    claim = _fake_claim_in(state["messages"])
    # 第四道闸：将来时承诺"这就/马上…带主人去X"但未调用任何工具 = 假完成（两闸盲区）
    promise = _fake_promise_in(state["messages"])
    if fake or claim or promise:
        if count >= MAX_REFLECTIONS:
            # Gate4 诚实兜底：预算耗尽且最终轮仍违规（伪造命令/虚假声称/空头承诺）——
            # 不接受谎言收尾，标记 fallback 让 server 层回放本对话最近一个
            # 通过质检轮的诚实回复作为最终输出（访客看到的是诚实内容而非假承诺）。
            violation = fake or ("声称完成但无工具调用" if claim else "承诺跳转但无工具调用")
            logger.info("[reflector] FALLBACK: 预算耗尽(%d/%d)且最终轮违规(%s)，诚实兜底",
                        count, MAX_REFLECTIONS, violation)
            return {"done": True, "fallback": True,
                    "reflection": f"反思预算已用尽({count}/{MAX_REFLECTIONS})且最终轮仍违规（{violation}），触发诚实兜底",
                    "reflection_count": count}
        if fake:
            correction = SystemMessage(content=(
                f"[Reflection 检查未通过：回复正文中出现了未调用工具的伪造命令（{fake} 前缀）。] 修正要求："
                f"页面跳转/特效/夜间模式只能通过调用对应工具完成——立即调用对应工具执行"
                f"（navigate_to/toggle_effect/toggle_dark_mode），工具返回后再按结果回复；"
                f"严禁在正文中输出任何命令前缀文本，之前输出的命令已作废且不会执行；"
                f"若因访客明确禁止等原因无法调用工具，如实拒绝并给出页面链接即可，不得虚构已完成。"
            ))
            logger.info("[reflector] FAKE_COMMAND: %s", fake)
            return {"messages": [correction], "done": False, "reflection": f"正文伪造命令 {fake}（未调用工具）", "reflection_count": count + 1}
        if promise:
            correction = SystemMessage(content=(
                "[Reflection 检查未通过：正文承诺'这就/马上/现在…带主人去X'式跳转，但本轮没有调用任何工具，跳转并不会真实发生。] 修正要求："
                "页面跳转只能通过调用 navigate_to 工具完成——立即调用该工具执行，工具返回后再按结果回复；"
                "若因访客明确禁止等原因无法调用工具，如实拒绝并给出页面链接即可，不得口头承诺无法兑现的跳转。"
            ))
            logger.info("[reflector] FAKE_PROMISE")
            return {"messages": [correction], "done": False, "reflection": "承诺跳转但无工具调用", "reflection_count": count + 1}
        correction = SystemMessage(content=(
            "[Reflection 检查未通过：正文声称已完成跳转/到达，但本轮没有调用任何工具，跳转并未真实发生。] 修正要求："
            "页面跳转只能通过调用 navigate_to 工具完成——立即调用该工具执行，工具返回后再按结果回复；"
            "若因访客明确禁止等原因无法调用工具，如实拒绝并给出页面链接即可，不得虚构已完成。"
        ))
        logger.info("[reflector] FAKE_CLAIM")
        return {"messages": [correction], "done": False, "reflection": "声称完成但无工具调用", "reflection_count": count + 1}

    last = state["messages"][-1]
    last_content = (getattr(last, "content", "") or "").strip()

    if intent == "chat":
        return {"done": bool(last_content), "reflection": "chat 快道路：非空检查通过" if last_content else "chat 回复为空", "reflection_count": count}

    if count >= MAX_REFLECTIONS:
        return {"done": True, "reflection": f"反思预算已用尽({count}/{MAX_REFLECTIONS})，接受当前结果", "reflection_count": count}

    llm = get_llm(temperature=0.0, max_tokens=200, timeout=30)
    resp = llm.invoke(_REFLECTOR_PROMPT.format(plan=state["plan"], trace=_build_trace(state["messages"])))
    raw = getattr(resp, "content", str(resp))
    if re.search(r"VERDICT\s*[:=]\s*REVISE", raw, re.IGNORECASE):
        m = re.search(r"NOTE\s*[:=]\s*(.+)", raw, re.IGNORECASE)
        note = m.group(1).strip() if m else "未按计划执行"
        correction = SystemMessage(content=(
            f"[Reflection 检查未通过：{note}] 修正要求："
            f"1) 若上次工具调用失败（如路径无效），立即用错误信息中给出的有效参数重试；"
            f"2) 不得向用户声称页面/数据不存在——先重试，重试仍失败才如实说明；"
            f"3) 你之前的回复已作废且不会展示，直接输出修正后的最终回复（重试成功则简短确认即可，不要重复之前的文字）。"
        ))
        logger.info("[reflector] REVISE: %s", note)
        return {"messages": [correction], "done": False, "reflection": note, "reflection_count": count + 1}

    logger.info("[reflector] PASS")
    return {"done": True, "reflection": raw, "reflection_count": count}


# ---------------------------------------------------------------------------
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
