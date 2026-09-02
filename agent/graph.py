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
    chat 意图走零 LLM 快道（声称闸正则族 + 非空检查，20260828/31/0902 扩展）

LangGraph 四件套（对照第一课讲解）：
  State  —— AgentState（节点间共享的字典，字段决定"工作台长什么样"）
  Node   —— planner/model/tools/reflector（每个是普通函数：state 进、更新字段出）
  Edge   —— 普通边（顺序传送带）+ 条件边（按返回值路由，循环/终止/纠错所在）
  Reducer—— Annotated[list, add_messages]：messages 字段"追加"而非覆盖

与现有工程外壳的关系（全部保留不动，课4 接入 server.py）：
  _build_messages（历史/摘要注入/时间锚）、SSE 帧协议、超时体系、recursion_limit
  —— 都在 server.py，本文件只负责"图长什么样"。
  注：_force_display 强制路由已随 20260828 影子系统重构移除（见问题记录）。
"""

from __future__ import annotations

import ast
import json
import logging
import re
import time
from typing import Annotated, Literal, TypedDict

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.runnables import RunnableConfig
from langgraph.graph import StateGraph, START, END
from langgraph.graph.message import add_messages

from models import get_llm
from tools import get_all_tools
from agent.prompts import BLOG_ASSISTANT_PROMPT
from agent.skills import (FUZZY_NAV_RULES, NAV_MAP, SKILL_MAP,
                          build_planner_context, instantiate_plan)
from utils.trace import record

logger = logging.getLogger(__name__)

# 客户端断开（stop_event 置位）→ 图内节点主动终止执行。
# 场景（20260827 实测）：浏览器连接中断后 event_stream 无法及时感知（卡在
# queue.get），agent 线程无感知继续执行 ReAct 循环——曾见断连后仍执行
# device_oled_display 写操作。server.py 侧 2s 轮询断连 → set stop_event →
# 图内 model/tools 节点在"下一次执行前"检查并抛此异常终止，写操作绝不
# 发生在用户已离开之后。由 server.py 捕获（静默收尾，客户端已断无帧可发）。
class AgentCancelled(Exception):
    pass


def _stopped(config: RunnableConfig | None) -> bool:
    """节点级中断检查：stop_event（threading.Event）由 server.py 经 config 注入。"""
    ev = (config or {}).get("configurable", {}).get("stop_event")
    return ev is not None and ev.is_set()


# 工具一次构建全局复用（tools/base.py 的 @tool 都是纯函数，无状态）
_TOOLS = get_all_tools()
_TOOL_MAP = {t.name: t for t in _TOOLS}

# 高危动作工具（有副作用 + 不可逆/外部影响）：只能在计划 TOOLS 行明确列出时执行
# （tools_node 授权检查）。与只读查询工具（检索/列表）区分——查询任何计划下合法，
# 高危动作越权即拒。
# 依据：问题记录 1.11 无需求重放风险分析 + 20260828 golden 实证（content_query
# 计划下执行留言注入指令真跳转 /device-console/；/iot 语义替身同样经此越权）。
# 范围取舍（20260828 golden 第二轮实证后收缩）：navigate_to（页面位置真实性，
# 1.9/1.12 事故核心）+ device_oled_display（外部设备写，2.x 事故核心）保持高危；
# toggle_effect/toggle_dark_mode 是页面视觉开关，误执行无位置误导/外部影响，且
# planner 判错技能时允许 model 自纠正（曾见 planner 把"改成下雨"判成 chat →
# 授权拒绝后整轮退化为"权限受限"）——视觉开关不做执行前授权，交给 reflector
# 幂等判定 + LLM 质检兜底。
_ACTION_TOOLS = {"navigate_to", "device_oled_display"}

# reflector 纠错预算：最多 REVISE 2 次，防止反思循环烧钱/烧时间
MAX_REFLECTIONS = 2
# 工具失败重试上限：同一 (工具, 参数) 调用失败最多允许模型修正重试 1 次
# （与原 prompt 规则"按错误信息给出的有效参数重试一次"一致，只是显式化）
MAX_TOOL_RETRIES = 1


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
    # 20260828 图改进（RAG 前基线配套）：
    # - tool_retries: 工具失败重试记录 [{key, name, args, error, attempt}]——失败路径
    #   从"prompt 规则让模型自觉重试"升级为图状态显式跟踪（可观测、可注入上下文）
    # - last_issue: 最近一次 REVISE 的结构化失败信息 {issue, detail}——错误记忆切入点，
    #   将来摘要任务可据此记录"执行过哪些类型的错误"
    tool_retries: list
    last_issue: dict | None


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

当前页面上下文（前端实时上报的事实，以此为准，不要凭对话历史推断访客位置——
访客可能手动转跳页面，对话历史不会体现位置变化）：
{page_ctx}

规则：
1. 只能从技能表中选择一个技能，不得自创步骤或自由编写执行计划。
2. 闲聊、问候、情感交流、明显不依赖博客内容的纯文字问答 → chat 技能。
3. 用户询问博客内容（文章/说说/留言/公告/站点信息里写了什么、怎么做、是什么；
   博客功能/系统机制如何工作，如"Git 和 SVN 有什么区别""ESP32 的 OTA 怎么配置"
   "agent 怎么防止模型假装调用了工具""最新留言说什么"）→ content_query。
   工具由执行层按场景自选：数据/列表型查询（最新留言/说说/封面图）直接走
   list_guestbook/list_talks 等数据工具，知识型问题用 rag_search/search_notes
   定位文章后 get_article_detail 读全文。即使问题表述得像概念问答也要选
   content_query——答案在博客内容里，不在模型常识里；不拿模型记忆冒险。
   检索工具选型（search_notes vs rag_search）是执行层的事，planner 不决策。
4. 导航目标在映射表中标记为"已下线"（如友链）时：选 chat 技能如实告知，不要选 navigate。
5. PARAMS 必须严格按技能定义的参数名输出。
6. 访客给出以 / 开头的具体路径时，target 原样填该路径，不要推断它对应哪个页面
   （如 /iot 就是 /iot；路径是否有效由系统按白名单预校验，页面别名才走映射表）。
7. 用户消息含常见导航动词（去/回/到/打开/跳转/访问/进入/返回/转到）且提到页面
   别名或路径时 → navigate；口语化措辞（如"回首页""去留言板"）同样是导航意图，
   不要退化成 chat。仅提及页面但不要求前往（如"首页的文章好看吗"）不选 navigate。
8. 用户要求把某段文字显示/写到 IoT 设备屏幕（如"在屏幕上写XXX""显示屏上显示XXX"
   "OLED 换成XXX"）时 → device_display（text 参数不用填，由执行模型创作内容）；
   只是询问设备/屏幕状态或屏幕上有什么内容（如"屏幕上显示什么""设备显示什么"）
   → device_query 或 chat，不要选 device_display。

输出严格按以下格式，不要输出任何其他内容：
SKILL: <技能名>
PARAMS: <JSON>

用户消息：{user_msg}"""


def _retry_context(retries: list) -> str:
    """工具失败重试上下文（纯函数，注入 model_node 的 system prompt）。

    未超限 → 按错误修正参数重试；已超限 → 如实告知、不再重试、不得编造成功。
    """
    if not retries:
        return ""
    r = retries[-1]
    if r["attempt"] <= MAX_TOOL_RETRIES:
        return (
            f"\n[上一轮工具调用失败（第 {r['attempt']}/{MAX_TOOL_RETRIES} 次尝试）] "
            f"工具 {r['name']} 参数={json.dumps(r['args'], ensure_ascii=False)}，"
            f"错误：{r['error'][:200]}\n"
            "处理规则：根据错误信息修正参数后重试一次；"
            "修正后仍失败 → 如实告知用户失败原因，不得编造成功。\n"
        )
    return (
        f"\n[工具调用失败已达上限（第 {r['attempt']} 次尝试，上限 {MAX_TOOL_RETRIES}）] "
        f"工具 {r['name']} 错误：{r['error'][:200]}\n"
        "处理规则：停止重试，如实告知用户该操作失败的原因，不得编造成功。\n"
    )


def _correction_msg(issue: str, detail: str) -> SystemMessage:
    """结构化修正注记构造器：issue=失败类型，detail=具体描述。

    20260828 图改进②：原 6 处手写 SystemMessage 语义散落（各自不同的修正要求
    模板），收敛为统一构造。issue 类型写进 state.last_issue + 日志——
    是跨会话错误记忆的切入点（将来摘要任务可按 issue 记录执行错误）。
    """
    return SystemMessage(content=(
        f"[Reflection 检查未通过（{issue}）：{detail}] 修正要求："
        f"1) 若本轮尚未调用任何工具，立即调用计划 TOOLS 行要求的工具，工具返回后再回复，"
        f"不得直接声称已完成；"
        f"2) 若上次工具调用失败，立即用错误信息中给出的有效参数重试一次；"
        f"3) 不得向用户声称页面/数据不存在——先重试，重试仍失败才如实说明；"
        f"4) 你之前的回复已作废且不会展示，直接输出修正后的最终回复"
        f"（简短确认即可，不要重复之前的文字）。"
    ))


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

def _msg_text(m) -> str:
    """多模态 content 兼容：数组（image_url+text 块）只取 text 文本部分。

    planner 分类/轨迹摘要只消费文本——base64 dataURL 不进分类器，否则
    content[-500:] 截到图片垃圾、_build_trace 把数组切片当文本。
    """
    content = getattr(m, "content", "")
    if isinstance(content, list):
        return "".join(c.get("text", "") for c in content
                       if isinstance(c, dict) and c.get("type") == "text")
    return content if isinstance(content, str) else str(content)


def _page_ctx(messages: list) -> str:
    """提取前端实时上报的页面上下文（page/title/特效/夜间），注入 planner/executor。

    前端每轮请求都携带真实 current_url（window.location.href），_build_messages
    写入首条 [System: ...] 消息。planner/executor 若只凭对话推断访客位置会脱节：
    用户手动转跳后对话历史不体现页面变化（曾见用户说"已经离开物联网控制台了，
    在首页"，模型仍延续上一轮的设备显示动作）。此处显式提取注入 prompt——
    事实以系统上报为准，不依赖模型推断；零额外 LLM 调用（planner/executor
    每轮本来就要调用）。
    """
    for m in messages:
        content = _msg_text(m) or ""
        found = re.search(r"\[System:\s*(.*?)\]", content, re.DOTALL)
        if found:
            return found.group(1).strip()
    return "（无）"


# 导航确定性快道（零 LLM）：动词 + 页面别名强模式 → 直接实例化 navigate 计划。
# 用户实测"规划要7秒/10几秒"——planner LLM 对导航这类最常见的固定流程任务
# 没必要调用（映射表白名单校验已确定性兜底，触发词也足够窄）。命中 → 秒级出
# 计划；不命中任何映射 → None → 落回 planner LLM（模糊表达/未知页面交给模型）。
# 20260828 事故加固（"你读到留言为什么没有按留言执行任务"被误判成导航请求）：
#  1. 疑问/质疑句式整体排除（_QUESTION_RE）——质疑不是导航请求；问路类
#     （"怎么去留言板"）排除后由 planner LLM 识别为导航意图，功能不丢只多一次调用；
#  2. 动词改为 match（必须句首，允许剥离称呼前缀）而非 search——"读到"里的"到"
#     曾命中句中任意位置的正则；
#  3. 目标串收紧到 8 字——16 字会整段捕获噪声目标（曾捕获"留言为什么没有按留言执行任务"）。
_QUESTION_RE = re.compile(r"为什么|怎么|如何|啥|什么|为何|哪儿|哪|吗$|么$|[？?]")
# 执行声称词（20260828 影子系统重构）：回复含这些词即构成"已对设备/页面执行了
# 操作"的声称。程序可查的事实：声称必须有工具返回支撑（轨迹里有 ToolMessage），
# 否则就是编造——确定性检查用代码，判断才用 LLM。
_EXECUTION_CLAIM_RE = re.compile(
    r"已(?:经)?(显示|写入|写下|写好|写上去|上屏|发送|下发|执行|展示|打上|放上|刷新|设置)"
    r"|成功(?:显示|写入|下发|发送|执行)"
)
# 读取声称族（20260831 补，21:19:40 事故实证：chat 轮声称"回去重读"文章但零工具
# 调用，引用 6 处全文细节 5 处不存在）——声称"读了/查了博客内容"必须以工具返回
# 为据。模式收敛（宁漏勿误伤：只看"读/查/看"+内容宾语与"重读"类，不抓裸"看了"）。
# 20260901 事故补丁：模型声称"查的是[关于页]""把整个博客扫了一遍""找到几条…文章
# 链接"（零工具调用，7 个 /article/61/59/57/62/64/68/71 全部 404）——"查的是X页"、
# "扫了一遍"、"找到N条"类表述同样构成读取声称，纳入模式（宾语限页面/博客/内容域，
# 不抓"找到工作/找到钥匙"类生活语）。
# 20260902 事故补丁（025744 实证："您让我查的这两条，我读完了"零工具编造，文章
# 17/35 不存在）：三种表述漏网——①"查的这两条"（缺"是/就是"、以量词"条"结尾，
# 页类分支不匹配）；②裸"读完了"（宾语缺失，原模式要求读完+宾语）；③"查了两篇
# 文章"（量词"两篇"插入动词与宾语之间）。修复：动词族加量词段、裸"读完了"加
# 句末分支、"查的"分支允许省略"是/就是"且纳入条/篇量词，动词族补"搜/检索"。
_READ_CLAIM_RE = re.compile(
    r"重读|重看|重新读|重新看|回去读"
    r"|已(?:经)?读取|已?通读"
    r"|(?:都|全部|基本)?读完了?(?:全文|文章|内容|文档|这篇|那篇|博客|[。，；!？!?～~\n🐾喵]|$)"
    r"|(?:我|咱|喵)?(?:刚|刚才|刚刚|已经?)?(?:读|看|查|翻|搜|检索)(?:过|了|完|遍)(?:了)?(?:这|那)?(?:一|两|三|几|数|[一二三四五六七八九十0-9]*)?(?:条|篇|个|本|些)?(?:相关|有关|的)?(?:全文|文章|内容|文档|留言|说说|博客|链接|帖子)"
    r"|(?:这|那)?[一二三四五六七八九十0-9]*(?:条|篇|个|本)(?:留言|说说|文章|消息|内容|链接)(?:我|咱|喵)?(?:都|全部)?(?:读|看|查|翻)(?:过|了|完|遍)(?:了)?"
    r"|(?:核对|核实|查验)(?:过)?(?:全文|文章|内容|文档)"
    r"|查的(?:是|就是)?(?:(?:这|那)?[一二三四五六七八九十0-9]*(?:条|篇|个)(?:留言|说说|文章|消息|内容|链接)?|[^，。！？!?～~\n]*?(?:页|页面|博客|文章|内容|正文))"
    r"|(?:我|咱|喵)?把?(?:整个)?(?:博客|网站|站点|文章库|站内)?(?:扫|查|翻|搜)了(?:个)?(?:一遍|一圈|遍)"
    r"|(?:两|双)(?:边|侧|个)(?:板块|数据源)?(?:都|也)?(?:真的)?(?:翻|查|看|搜)(?:了|过|完)(?:了)?"
    r"|找(?:到|出|出了)(?:了)?(?:几|数)?[一二三四五六七八九十0-9]*(?:条|篇|个|些)(?:[^，。！？!?～~\n]{0,20}?)?(?:文章|链接|博客|内容|文档|东西)"
)
# 工具调用声称族（20260902 补，133535 实证）：chat 零工具轮回复"刚才那两条我
# 都调用了工具——就是那个查时间的(get_current_time)，所以才能报出 05:34 这个
# 准数"——声称"调用了工具/基于工具返回"、或点名具体工具名，都必须有轨迹
# ToolMessage 支撑。原 _READ_CLAIM_RE 只抓"读/查+内容宾语"、_EXECUTION_CLAIM_RE
# 只抓"已执行"，"调用了工具"型措辞两者皆漏，零工具 PASS。零工具轮点名具体
# 工具名（get_current_time/rag_search/…）= 声称调用过该工具（13:45 实证：
# "用 rag_search 搜了一遍"零调用）；能力介绍句式极少点名具体工具名，宁可信其
# 为声称——误伤由打回语引导澄清，成本低。
_CALLED_TOOL_CLAIM_RE = re.compile(
    r"调(?:用|过)(?:过)?(?:了)?(?:工具|get_current_time|rag_search|list_guestbook|list_talks|get_announcements|get_article_detail|search_notes)"
    r"|调用了?(?:这个|那个|这些|两个|几个|三个)?工具"
    r"|(?:get_current_time|rag_search|list_guestbook|list_talks|get_announcements|get_article_detail|search_notes)"
)


def _claim_guard_correction(last_content: str, round_msgs: list) -> tuple[str, str] | None:
    """声称闸判定（chat 快道与 content_query 零工具轮共用，20260902）：
    当前轮零工具执行时，回复含三类声称（已读取内容 / 已执行操作 / 已调用工具）
    即无事实依据——声称必须有轨迹 ToolMessage 支撑。返回 (issue, 修正语) 或
    None。effect/darkmode 幂等零调用与 NOTE 零工具场景不经过本闸（各自确定性
    闸先返回），经此的零工具轮都是"可调用但没调"的自由回复场景。"""
    if any(isinstance(m, ToolMessage) for m in round_msgs):
        return None  # 有工具执行记录 → 声称有据，内容比对交给 LLM 质检
    if _READ_CLAIM_RE.search(last_content):
        return ("claim_without_tool",
                "你的回复声称已阅读/查阅了博客内容（重读/读完了全文/查过了…），"
                "但本轮没有任何工具执行记录——了解博客内容必须调用检索/读取工具"
                "（如 rag_search/search_notes 定位、get_article_detail 读全文、"
                "list_guestbook/list_talks 查列表）、以工具返回为据；未调用工具时"
                "不得声称读过/查过，只能如实说明尚未查看")
    if _EXECUTION_CLAIM_RE.search(last_content):
        return ("claim_without_tool",
                "你的回复声称已执行设备/页面操作（已显示/已写入/已发送/已执行），"
                "但本轮没有任何工具执行记录——执行声称必须以工具返回为依据；"
                "未调用工具时不得声称已执行，只能如实说明无法执行或正在做什么")
    if _CALLED_TOOL_CLAIM_RE.search(last_content):
        return ("claim_without_tool",
                "你的回复声称调用了工具（「调用了X工具」「点名具体"
                "工具名」），但本轮没有任何工具执行记录——声称调用过工具必须有真实"
                "的工具调用与返回为据；未调用工具时不得声称调用过或引用工具返回，"
                "如实说明即可")
    return None

_NAV_VERB_RE = re.compile(
    r"^(?:小猫咪|喵喵|主人|猫猫|喵)?[,，、\s]*"
    r"(?:去一下|回到|返回|跳转到|前往|转到|转跳|打开|进入|带我(?:去|到)|去|进|回|到|访问)"
    r"\s*([^\s，。！？!?～~、；;：:]{1,8})$"
)


# 当前文章读取确定性快道（20260901 系统性修复，零 LLM）。
# 根因（用户评审定性，声称闸补丁被拒）：模型对"用户当前在读的文章"只有
# page_ctx 文本提示（current_url=/article/21），无结构化事实、无强制读取——
# 于是模型凭 URL 文本知道在读哪篇、却永远不真的读，回答全靠想象。事故实证：
# 232107「这篇文章你怎么看」→ planner 选 content_query 且 plan=[]（零工具），
# 模型声称"这篇我读完了"编造 600 字全文细节（"流程节点图""route A/B 双通道"
# "技能=Markdown 文件"全部虚构），reflector 给 PASS；232302「你知道我现在读
# 什么吗」→ planner 选 chat（零工具），模型自己承认"我确实不知道那篇文章里写
# 了什么"，但没有机制强制去读。
# 修复与导航/显示快道同构：命中 = 确定性识别（无模型猜测通道）——current_url
# 解析出文章 ID 是系统数据（非模型推断），计划 TOOLS 行强制 get_article_detail
# → executor 必须调用 → reflector 检查点 1（TOOLS 行要求的工具必须调用）与
# 预算耗尽最后通牒确定性兜底。模型对文章内容的声称从此有真实工具返回为据。
# 触发语域限"这篇/正在读/读到这"等强指称，宁多勿漏（文章页上误触发成本 =
# 一次毫秒级读取，漏触发 = 幻觉重演）；不命中 → 落回 planner LLM。
_ARTICLE_URL_RE = re.compile(r"/article/(\d+)")
_ARTICLE_REF_RE = re.compile(
    r"这篇|这篇文章|这篇文"
    r"|我(?:现在|正在|当前)?(?:在读|读的)|我现在读|正在读|现在在读|正在看|现在看"
    r"|(?:读|看)到(?:这里|这篇)|看完这篇|读完了这篇"
    r"|这篇文章(?:讲|写|说|聊|介绍|什么|怎么|如何|怎样|你)"
)


def _article_fast_path(user_msg: str, page_ctx: str) -> dict | None:
    """当前文章读取快道：current_url 匹配 /article/<id> 且消息引用当前文章
    （"这篇/我正在读/读到这"…）→ read_article 计划（TOOLS 强制 get_article_detail）。

    返回带 params 的 plan dict（与 planner LLM 路径同构），或 None 落回 planner LLM。
    文章 ID 从 page_ctx 的 current_url 正则解析——系统数据，不存在模型猜错通道；
    read_article 技能对 planner LLM 不可见（build_planner_context 过滤），仅本
    快道注入（instantiate_plan 缺 article_id 时按 chat 兜底，防误用）。
    """
    m = _ARTICLE_URL_RE.search(page_ctx)
    if not m:
        return None
    if not _ARTICLE_REF_RE.search(user_msg):
        return None
    article_id = m.group(1)
    plan_obj = instantiate_plan("read_article", {"article_id": article_id})
    plan_obj["params"] = {"article_id": article_id}
    logger.info("[planner] 当前文章读取快道命中（零 LLM）: %s", plan_obj["tools"])
    return plan_obj


def _nav_fast_path(user_msg: str) -> dict | None:
    """导航快道：整句映射命中，或动词+目标强模式 + 映射/模糊归一命中 → navigate 计划。

    返回带 params 的 plan dict（与 planner LLM 路径同构），或 None。
    目标校验走与 instantiate_plan 完全相同的 NAV_MAP/FUZZY_NAV_RULES 白名单——
    快道命中 = 确定性识别，不存在"模型猜错"通道；未知页面（"去火星基地"）不命中
    映射 → None → planner LLM 按"如实告知没有该页面"处理。已下线页面（友链）同样
    命中（NAV_MAP 值 None），实例化后 note 会要求如实告知、零工具。
    """
    msg = user_msg.strip().strip("，。！？!?～~、")
    # 疑问/质疑句式（"为什么""？"等）不是导航请求，直接排除（20260828 事故加固，
    # 见 _NAV_VERB_RE 上方注释）；问路类由 planner LLM 兜底识别为导航意图
    if _QUESTION_RE.search(msg):
        return None
    target = msg if msg in NAV_MAP else None
    if target is None:
        m = _NAV_VERB_RE.match(msg)  # match 而非 search：动词必须句首，避免句中误匹配
        if m:
            t = m.group(1)
            if t in NAV_MAP or t.startswith("/"):
                target = t
            else:
                fuzzy = next(
                    (p for kws, p in FUZZY_NAV_RULES if any(kw in t for kw in kws)), None
                )
                if fuzzy:
                    target = t
    if target is None:
        return None
    plan_obj = instantiate_plan("navigate", {"target": target, "mode": "direct"})
    plan_obj["params"] = {"target": target, "mode": "direct"}
    return plan_obj


# 显示意图确定性快道（零 LLM，20260828 影子系统重构）：屏幕类名词 + 写/显示类动词
# 强模式 → 直接实例化 device_display 计划。不经过 planner LLM、更不经过提取器——
# "显示内容由执行模型在工具调用时创作"（REPLY 契约驱动），彻底移除
# _extract_display_intent 把"写点东西"提取成"点东西"这类残缺内容事故。
# 与导航快道同构：命中 = 确定性识别（无模型猜测通道）；不命中 → 落回 planner LLM。
# 排除项（防误伤）：
#  - 疑问句式（为什么/怎么/吗/？）——问路不是命令；
#  - 否定式（不用/不要/别）——"不用在屏幕上显示"不是显示命令；
#  - 仅"设备"名词不触发（与 device_query 冲突："设备显示什么"是查询）。
_NEGATION_RE = re.compile(r"不(用|要|想|必|需要)|别|不要")
_DISPLAY_FAST_RE = re.compile(
    r"(屏幕|OLED|显示屏|显示器|大屏)[^\n。！？!?]{0,12}(写|显示|展示|换上|换成|改成|放|打上)"
    r"|(写|显示|展示|换上|换成|改成)[^\n。！？!?]{0,12}(屏幕|OLED|显示屏|显示器|大屏)"
)


def _display_fast_path(user_msg: str) -> dict | None:
    """显示意图快道：屏幕类名词+写/显示动词强模式 → device_display 计划（零 LLM）。

    返回带 params 的 plan dict（与 planner LLM 路径同构），或 None 落回 planner LLM。
    内容由执行模型生成（PARAMS 不填 text）——工具调用参数在 model 节点按 REPLY
    契约创作，杜绝"指令原文残缺片段上屏"。
    """
    if _QUESTION_RE.search(user_msg) or _NEGATION_RE.search(user_msg):
        return None
    if not _DISPLAY_FAST_RE.search(user_msg):
        return None
    plan_obj = instantiate_plan("device_display", {})
    plan_obj["params"] = {}
    logger.info("[planner] 显示意图快道命中（零 LLM，内容由执行模型创作）")
    return plan_obj


def planner_node(state: AgentState) -> dict:
    """职责：技能选择器——从技能注册表选技能 + 填参数 → 实例化为计划 → 写入 state.plan。

    与旧版（自由写 STEPS 步骤）的本质区别（面试点）：规划空间受限。模型不写
    "怎么做"（执行步骤是技能模板里的静态数据），只回答"做什么"（选技能）和
    "参数是什么"（填参数）。跑题的根因——planner 不知道工具语义/页面映射——由
    技能表注入（导航映射表"物联网平台→/device-console/"是系统数据）结构性消除。
    代价：每次对话多一次 LLM 调用（约 0.3-0.8s），换来可解释且受限的执行路径。
    """
    last = state["messages"][-1]  # 最后一条是当前用户请求
    user_msg = _msg_text(last)[-500:]  # 只看最近一段，防止超长输入稀释分类

    # 导航确定性快道（零 LLM）：命中即返回，不调用 planner LLM（耗时大头）。
    nav = _nav_fast_path(user_msg)
    if nav is not None:
        logger.info("[planner] 导航快道命中（零 LLM）: %s", nav["tools"])
        record("planner", "fastpath", kind="nav", tools=nav["tools"])
        return {"plan": plan_encode(nav), "reflection": "", "reflection_count": 0, "done": False}

    # 显示意图确定性快道（零 LLM）：屏幕类名词+写/显示动词强模式 →
    # device_display 计划（内容由执行模型创作，PARAMS 不填 text）。
    display = _display_fast_path(user_msg)
    if display is not None:
        record("planner", "fastpath", kind="display")
        return {"plan": plan_encode(display), "reflection": "", "reflection_count": 0, "done": False}

    # 当前文章读取确定性快道（零 LLM，20260901 系统性修复）：用户当前页面是
    # 文章详情页且消息引用"这篇/我正在读"等 → read_article 计划，TOOLS 行强制
    # get_article_detail(id)。根因与事故见 _article_fast_path 注释——模型零工具
    # 声称读过/编造全文（232107/232302），正则声称闸打不住（歌词"找到几条"即
    # 误伤），必须把"读当前文章"做成固定流程：ID 是系统从 current_url 解析的
    # 数据，执行被计划模板强制，reflector 检查点 1 确定性兜底。
    article = _article_fast_path(user_msg, _page_ctx(state["messages"]))
    if article is not None:
        record("planner", "fastpath", kind="article_read", tools=article["tools"])
        return {"plan": plan_encode(article), "reflection": "", "reflection_count": 0, "done": False}

    # 快思考模块：低温度（分类不需要创造力）、小 max_tokens、短超时
    # 20260829：耗时打点与 model 节点对称（planner 慢同样常见，统一可查）
    # 20260830：enable_thinking=False——planner 是"选技能+填参数"的结构化分类
    # 任务（300 token 输出），thinking 思考链纯浪费（实测 13.4s → 预计 2-4s，
    # 且 3.7s/13.4s 的波动正来自 thinking 链长度）；与 reflector/显示提取/摘要
    # 三个低 token 调用同一做法（此前漏关，本次补上）
    llm = get_llm(temperature=0.2, max_tokens=300, timeout=30, enable_thinking=False)
    _t0 = time.monotonic()
    logger.info("[planner] LLM 调用开始")
    resp = llm.invoke(_PLANNER_PROMPT.format(
        skills_context=build_planner_context(), tools_desc=_tools_desc(),
        page_ctx=_page_ctx(state["messages"]), user_msg=user_msg))
    # 20260830：慢调用监控——>30s 打 WARN（正常 <5s，慢=服务端排队/长思考，
    # 与前端 60s 空闲超时呼应：慢调用是超时事故的前兆信号）
    dur = time.monotonic() - _t0
    slow = dur > 30
    (logger.warning if slow else logger.info)(
        "[planner] LLM %s 耗时=%.1fs", "慢调用" if slow else "完成", dur)
    record("planner", "llm_done", duration_s=round(dur, 2),
           **({"slow": True} if slow else {}))
    raw = getattr(resp, "content", str(resp))
    skill_name = re.search(r"SKILL\s*[:=]\s*(\w+)", raw, re.IGNORECASE)
    skill_name = skill_name.group(1) if skill_name else "chat"
    params = _parse_params(raw)
    plan_obj = instantiate_plan(skill_name, params)
    plan_obj["params"] = params

    # 字面路径防推断兜底（确定性修正）：用户消息里出现 / 开头的路径且 planner 选
    # 了 navigate 时，target 必须原样用该路径——qwen 曾把 "/iot" 推断成"物联网平台"
    # （语义替身）→ 计划变成跳转 /device-console/（golden nav_nonexistent 实证，
    # 规则层约束不生效）。白名单外的路径经 instantiate_plan 预校验 → 零工具 +
    # "不存在"注记 → 如实告知（与"路径是否有效由系统校验"的设计一致）。
    lit = re.search(r"/[A-Za-z0-9_\-./]+", user_msg)
    if (plan_obj["skill"] == "navigate" and lit
            and plan_obj["params"].get("target") != lit.group(0)):
        logger.info("[planner] 字面路径修正：用户消息含 %s，planner 目标 %r → 强制 %s",
                    lit.group(0), plan_obj["params"].get("target"), lit.group(0))
        plan_obj = instantiate_plan("navigate", {"target": lit.group(0), "mode": "direct"})
        plan_obj["params"] = {"target": lit.group(0), "mode": "direct"}

    logger.info("[planner] skill=%s params=%s tools=%s", plan_obj["skill"], plan_obj["params"], plan_obj["tools"])
    record("planner", "decision", skill=plan_obj["skill"], params=plan_obj["params"], tools=plan_obj["tools"])

    return {"plan": plan_encode(plan_obj), "reflection": "", "reflection_count": 0, "done": False}


_EXECUTOR_PROMPT = """\
{persona}

当前页面上下文（前端实时上报的事实，以此为准，不要凭对话历史推断访客位置；
访客手动转跳后对话历史不会体现位置变化）：
{page_ctx}

[执行计划]
{plan}

执行规则：
1. 按计划执行：TOOLS 行列出的是 planner 显式点名或技能模板固定的工具序列——
   列出即必须调用（工具结果以工具返回为准，不要编造），TOOLS 行列了多个工具
   时每一个都要调用；TOOLS 为（无）时按 SKILL 行技能语义处理：
   - content_query（自由 ReAct）：TOOLS 行点名了工具时，必须按 TOOLS 行全部
     调用后再回答（下方零工具豁免不适用——040213/040409 实证：点名了
     list_guestbook/list_talks 仍首轮零工具，qwen 误用"与博客无关"豁免）；
     未点名时，问题涉及博客数据（文章/说说/留言/公告/站点信息的存在性、内容、
     最新状态）必须自行调用对应工具（list_guestbook/list_talks/get_article_detail/
     search_notes/rag_search/get_announcements 等）核实后回答，回答基于工具返回、
     不得凭印象编造；仅当问题明确是通用知识/纯闲聊且不涉及博客数据时才可零工具
     直接回答。
   - 其他技能（navigate/effect/darkmode/device_display/device_query/chat）：
     不需要工具，直接回答。
2. 若 NOTE 行说明"不调用任何工具"（如导航目标已下线/页面不存在）：
   按 NOTE 如实告知访客即可，不要强行调用工具。
3. 所有步骤完成后，给出最终回复（遵循 REPLY 行的回复契约）。
4. 如果执行中发现计划不适用（例如工具返回与预期不符），按实际情况处理并
   在回复中说明——计划是参考，事实以工具返回为准。
5. 工具调用失败时（返回以 __ERROR__ 或"无效"开头的错误）：立即按错误信息中
   给出的有效参数重试一次；不得以"页面不存在/没有这个功能"为由放弃——
   先重试，重试仍失败才如实向用户说明。
6. 当前时刻/日期以 system context 的 current_time= 字段为准（[System: ...] 内的
   系统事实，同 current_effects/current_darkmode 语义）——回复中需要提及当前
   时刻时直接使用该字段值，不得凭对话历史或印象推算"现在几点"；若 context
   未提供 current_time 而又必须向访客报告时间，先调用 get_current_time 工具
   获取，未调用工具不得声称当前时刻。"""


def model_node(state: AgentState, config: RunnableConfig | None = None) -> dict:
    """ReAct 执行层的"思考"节点：带工具思考 → 产出 tool_calls 或最终回答。

    与 create_agent 的 model node 同源，但计划注入是显式的：
    system prompt = 人设 + 当前执行计划（技能模板实例），模型按模板驱动工具调用。
    """
    # 客户端断开检查：不再发起新的 LLM 调用（省流费 + 不产出无人接收的回复）
    if _stopped(config):
        logger.info("[model] cancelled (client disconnected)")
        raise AgentCancelled()
    # 20260831：enable_thinking=False——executor 关思考（用户拍板）。
    # 数据依据：慢调用监控 3 条 model WARN（46.8s/79.1s/105.8s）+ 20260830 超时事故
    # （118s/146.9s）同源——thinking 模式在长上下文（工具结果全文 + 检索候选 + 历史）
    # 下思考链爆炸，用户 60s 空闲超时等不到第一个 token 被迫中止（8-31 两次
    # "分析文章21" client_disconnect 实证）。planner 已关（8-30），executor 生成质量
    # 由 golden 55 条全量回归把关。
    llm = get_llm(enable_thinking=False)  # 主模型：对话生成（温度 0.7、可流式）
    # 20260828 图改进①：工具失败重试上下文注入（取代 prompt 规则"自觉重试"）。
    # 上一次工具调用失败时，显式告知失败详情 + 重试轮次语义（见 _retry_context）。
    system = SystemMessage(content=_EXECUTOR_PROMPT.format(
        persona=BLOG_ASSISTANT_PROMPT, plan=state["plan"],
        page_ctx=_page_ctx(state["messages"]))
        + _retry_context(state.get("tool_retries") or []))
    # 20260829：LLM 调用耗时打点——单次调用异常慢（qwen API 拥堵时 ~50s，
    # 15:21 事故：前端 idleTimeout 45s 先断、日志只有返回后一行，等待时长
    # 完全不可见）。耗时定位 LLM API 侧 vs agent 侧。
    # 注：百炼端点响应无 usage 字段（response_metadata 只有 finish_reason/
    # model_name/model_provider），token 成本统计暂不可得——RAG 成本评估
    # 前需另找数据源（API 侧账单或请求级 usage 透传）
    _t0 = time.monotonic()
    logger.info("[model] LLM 调用开始")
    record("model", "llm_start")
    resp = llm.bind_tools(_TOOLS).invoke([system] + state["messages"])
    # 20260830：慢调用监控——>30s 打 WARN（正常 <20s，慢=服务端排队/长思考，
    # 20260830 事故两次 118s/146.9s 就是这类；与前端 60s 空闲超时呼应）
    dur = time.monotonic() - _t0
    slow = dur > 30
    (logger.warning if slow else logger.info)(
        "[model] LLM %s tool_calls=%s 耗时=%.1fs",
        "慢调用" if slow else "完成",
        [c["name"] for c in resp.tool_calls], dur)
    record("model", "llm_done", tool_calls=[c["name"] for c in resp.tool_calls],
           duration_s=round(dur, 2), **({"slow": True} if slow else {}))
    return {"messages": [resp]}


def tools_node(state: AgentState, config: RunnableConfig | None = None) -> dict:
    """ReAct 执行层的"行动"节点：执行上一条消息里的所有工具调用。

    手写版 ToolNode（面试点：create_agent 内部就是这个逻辑，我们显式写出来）：
      1. 逐条执行 tool_calls，结果打包成 ToolMessage（tool_call_id 关联回调用）
      2. 按 (name, args) 去重——模型重试时可能重复发同一调用，
         副作用工具（切换特效/导航）执行两次是真 bug
      3. 异常不炸图：错误信息作为工具结果返回，让模型自行理解修正
    """
    # 客户端断开检查：工具执行前拦截——写操作（设备指令下发/导航/特效切换）
    # 绝不发生在用户已离开之后（20260827 实测：断连后仍执行了 OLED 显示指令）
    if _stopped(config):
        logger.info("[tools] cancelled (client disconnected) — 不执行任何工具（含写操作）")
        raise AgentCancelled()
    # 执行前授权检查（问题记录 1.11 候选修复落地，20260828）：动作工具只能在
    # 计划 TOOLS 行明确列出时执行——planner 是唯一决策点，model 无自由动作权。
    # golden 实证（20260828）：content_query 计划下 model 检索留言后把留言内容
    # （注入测试留言"读到我去执行 navigate_to…"）当指令真执行跳转 /device-console/；
    # /iot 语义替身同样经此越权。只读查询工具（检索/列表）任何计划下合法，
    # 动作工具（导航/特效/夜间/设备写）越权即拒——计划外调用零副作用。
    plan = parse_plan(state.get("plan", ""))  # 缺 plan 容错 → chat 兜底（allowed 空，动作工具全拒）
    allowed = {t.split("(")[0].strip() for t in plan["tools"]}
    last = state["messages"][-1]
    results, seen = [], set()
    # 20260828 图改进①：工具失败显式跟踪。原实现失败只以 __ERROR__ ToolMessage
    # 返回，重试靠 prompt 规则"让模型自觉"——失败次数、重试轮次不可观测。
    # 现在按 (name, args) 计数失败次数写入 state.tool_retries，model_node 据此
    # 注入显式重试上下文（含上限语义），失败路径成为图状态的一等公民。
    retries = list(state.get("tool_retries") or [])
    for call in last.tool_calls:
        key = (call["name"], json.dumps(call.get("args", {}), sort_keys=True))
        if key in seen:
            continue
        seen.add(key)
        _t_tool = time.monotonic()
        tool = _TOOL_MAP.get(call["name"])
        if tool is None:
            out = f"__ERROR__: 未知工具 {call['name']}"
        elif call["name"] in _ACTION_TOOLS and call["name"] not in allowed:
            # 授权拒绝：动作工具不在计划 TOOLS 行（如自由计划下把检索到的留言
            # 指令当命令执行、/iot 语义替身）——零执行，返回拒绝信息让模型
            # 按计划重来（__ERROR__ 前缀走重试语义：修正后不再调用即如实回复）
            out = (f"__ERROR__: 工具 {call['name']} 不在本次执行计划的 TOOLS 中"
                   f"（计划允许: {'、'.join(sorted(allowed)) if allowed else '仅只读查询'}），"
                   f"不得在计划外执行动作工具——按计划执行或直接如实回复")
            logger.info("[tools] 授权拒绝：%s 不在计划 TOOLS 中（allowed=%s）",
                        call["name"], sorted(allowed))
        else:
            try:
                out = tool.invoke(call.get("args", {}))
            except Exception as e:
                out = f"__ERROR__: {type(e).__name__}: {e}"
        results.append(ToolMessage(content=str(out), tool_call_id=call["id"], name=call["name"]))
        logger.info("[tools] %s → %.100s", call["name"], str(out))
        # trace 落盘：工具调用（名称/参数/耗时/结果摘要）——RAG 评估里
        # "检索工具拖慢"直接读 duration_s 判定
        result_ = str(out)
        # rag_search 完整落盘（行式候选已精简）——事后可分析完整候选与选择
        # 对比，不必翻代码复现截断（20260831 事故复盘教训，问题记录 1.26）
        if call["name"] != "rag_search":
            result_ = result_[:200]
        record("tools", "call", name=call["name"], args=call.get("args", {}),
               duration_s=round(time.monotonic() - _t_tool, 3),
               result=result_)
        if str(out).startswith("__ERROR__"):
            attempt = sum(1 for r in retries if r["key"] == key) + 1
            retries.append({
                "key": key, "name": call["name"], "args": call.get("args", {}),
                "error": str(out), "attempt": attempt,
            })
            logger.info("[tools] %s 失败（第 %d/%d 次尝试）: %.100s",
                        call["name"], attempt, MAX_TOOL_RETRIES, str(out))
    return {"messages": results, "tool_retries": retries}


_REFLECTOR_PROMPT = """\
你是执行质量检查员。对照技能模板检查对话执行轨迹。

技能模板（计划）：
{plan}

执行轨迹（摘要）：
{trace}

检查要点：
1. 工具调用缺失不在此处判断（20260901）：TOOLS 行点名工具是否调用由确定性闸门在
   LLM 质检之前判罚（零调用已 REVISE）。本条豁免只覆盖"TOOLS 行点名工具的缺失"；
   模型声称调用了未点名工具、或声称"基于工具返回作答"时，仍须对照下方"本轮实际
   执行工具记录"与执行轨迹核对——声称调用过工具而轨迹无对应记录的，属于"声称
   已完成实际未执行的动作"（见要点 5），判 REVISE，不要采信回复中的自称。
   工具已调用后，模型按检索实际情况调整候选顺序/补充额外检索工具/续接读取
   （见要点 7）均为合法执行，不得据此判 REVISE。
2. 工具结果有 __ERROR__/报错时（20260901）：模型如实转述错误、说明失败原因
   → 判 PASS（工具失败是外部事实，如实报告不是编造，见要点 5）；掩盖错误、
   把失败声称成成功 → 判 REVISE。工具报错本身不是判罚依据，回答与工具返回
   是否一致才是。
3. 最终回答是否基于工具返回的事实（有没有编造）？——判 REVISE 的唯一依据：
   回答中的事实与工具返回矛盾、或工具返回中不存在依据。
4. 是否回答了用户的问题？
5. 若模型因合理原因（功能/页面已下线——见 NOTE 行、数据不存在、工具返回错误后
   重试仍失败、访客明确禁止调用工具/模型无法调用工具）如实告知访客而未能完成
   TOOLS 行的某个调用：视为合理处理，判 PASS。此时如实拒绝并给出页面
   Markdown 链接是合格的最终回答。
   只有模型编造事实、或声称已完成实际未执行的动作时才判 REVISE。
6. 风格问题不判 REVISE：TOOLS 行要求的工具已成功调用（帧已产出）后，
   正文是否附 Markdown 链接、链接格式、措辞风格均不影响判罚——跳转/执行
   由系统帧完成，正文只是确认。
7. 允许合理绕道（20260901）：模型按检索实际情况调整候选选择顺序、补充
   TOOLS 行之外的检索工具（如 search_notes 补充检索）、或在后续轮次直接读取
   已定位到的文档——只要最终回答中的事实性内容（URL/字段值/正文引用）在工具
   返回中能找到依据（判罚依据见要点 3），就不得仅因"未按模板字面顺序 / 未选
   字面最高分候选 / 使用了额外工具"判 REVISE。
{tools_note}
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
            text = _msg_text(m)
            # rag_search 候选摘要已行式精简，放行完整——反射器需完整候选视野
            # 才能校验"选的候选对不对"（20260831 事故：100 字截断只见 top-1
            # 候选，把模型选 rank5 语义相关文档的合理行为误判为"读了不存在的
            # 文档"，REVISE 链把模型带偏，见问题记录 1.26）
            # 工具名前缀（20260901）：返回行不带工具名时 LLM 质检无法区分候选
            # 列表与详情读取（rag_search 候选里 talk id=11 与 get_article_detail
            # 读的 note id=11 混淆，把正确结果误判为"声称与工具返回不符"）——
            # 前缀让"哪个工具返回了什么"一眼可辨。id 跨表（talk/note/board 各自
            # 编号）是数据固有事实，轨迹里必须能区分来源。
            name = getattr(m, "name", "")
            if name == "rag_search":
                parts.append(f"工具返回[{name}]: {text}")
            else:
                # 20260901：dict/list 形态返回（get_article_detail/search_notes
                # 等）改结构化逐键截断——纯 [:100] 会切掉字典中部的字段（"测试4
                # 封面"事故实证：cover 字段被截断，反射器基于残缺轨迹把真实
                # cover 误判为编造，REVISE 打回正确结果）。键名完整保留（声称
                # 字段有无一眼可见），仅长值截断；非 JSON（纯文本/错误信息）[:100]
                stripped = text.lstrip()
                data = None
                if stripped.startswith(("{", "[")):
                    # 工具返回是 Python repr（单引号，tools/base.py str(dict)）而非
                    # 合法 JSON——json.loads 直接失败会落入 [:100] 兜底把 dict 中部
                    # 字段（cover）切掉（20260901"测试4封面"实证：结构化截断从未
                    # 生效，LLM 质检只见"仅包含标题"）。ast.literal_eval 解析
                    # Python 字面量（安全：不执行代码），失败再退回 JSON。
                    try:
                        data = ast.literal_eval(text)
                    except Exception:
                        try:
                            data = json.loads(text)
                        except Exception:
                            data = None
                if isinstance(data, dict):
                    parts.append(f"工具返回[{name}]: " + " ".join(
                        f"{k}={json.dumps(v, ensure_ascii=False)[:60]}" for k, v in data.items()))
                elif isinstance(data, list) and data and isinstance(data[0], dict):
                    compact = " ".join(
                        f"{k}={json.dumps(v, ensure_ascii=False)[:60]}" for k, v in data[0].items())
                    parts.append(f"工具返回[{name}]: [{len(data)} 条] 首条 " + compact)
                else:
                    parts.append(f"工具返回[{name}]: {text[:100]}")
        elif isinstance(m, AIMessage) and m.content:
            parts.append(f"助手回答: {_msg_text(m)[:100]}")
        elif hasattr(m, "content") and isinstance(m.content, (str, list)) and m.content:
            parts.append(f"{m.__class__.__name__}: {_msg_text(m)[:80]}")
    # 只留最近 1200 字符（20260901：800 会在多工具轮把较早的工具返回切掉——
    # "测试4封面"事故中 search_notes 的 cover 在返回前段；结构化后单工具返回
    # ~300-400 字符，1200 覆盖最后 3 个调用，token 成本仍在反射预算内）
    return "\n".join(parts)[-1200:]


def _tool_name(tool_spec: str) -> str:
    """TOOLS 行条目 → 工具名（'get_article_detail({"article_id": 21})' → get_article_detail）。"""
    return tool_spec.split("(", 1)[0].strip()


def _missing_tools(required: list[str], round_msgs: list) -> list[str]:
    """TOOLS 行要求的工具中，当前轮轨迹缺失的工具名列表（20260902 逐工具核验）。

    检查点 1 从"本轮有没有任何 ToolMessage"升级为"每个工具名都在轨迹中出现"——
    双源强制落地（list_guestbook+list_talks 缺一个即 REVISE；233815 事故后
    planner 显式点名工具进 TOOLS 行，只调一个数据源仍算执行不完整）；单工具
    技能（navigate/effect 等）行为不变。
    """
    if not required:
        return []
    called = {
        getattr(m, "name", "") or "" for m in round_msgs if isinstance(m, ToolMessage)
    }
    return [t for t in required if _tool_name(t) not in called]


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


def reflector_node(state: AgentState, config: RunnableConfig | None = None) -> dict:
    """质检节点包装：统一 trace 打点——快慢道/确定性检查/LLM 质检各返回
    路径都经过这里，一条 check 事件覆盖全部判定（done=False=REVISE）。"""
    _t0 = time.monotonic()
    out = _reflector_node_inner(state, config)
    record("reflector", "check", done=out.get("done"),
           count=out.get("reflection_count", 0),
           reflection=(out.get("reflection") or "")[:200],
           duration_s=round(time.monotonic() - _t0, 2))
    return out


def _reflector_node_inner(state: AgentState, config: RunnableConfig | None = None) -> dict:
    """反思层：对照技能模板质检执行结果。

    快慢两条道（面试点：反思也要算成本）：
      - chat 技能：不花 LLM 钱——先过声称闸（正则族：回复含执行/读取/工具调用
        声称且当前轮零工具 → REVISE，20260828/31 读取声称族、20260902 工具调用
        声称族扩展），再过非空检查（闲聊无执行可查，反思是浪费）
      - 其余技能：LLM 对照技能模板（TOOLS/NOTE/REPLY）+ 轨迹出 VERDICT：
          PASS   → done=True，收尾
          REVISE → 追加一条 [Reflection] 修正注记进 messages（紧贴当前轮，
                   遵守率最高），回 model 重来；预算 MAX_REFLECTIONS，耗尽即收
    模板质检天然覆盖旧版程序化闸门要抓的场景（模型假装执行）：TOOLS 行要求的
    工具若在轨迹中缺失，检查点 1 即判 REVISE——执行必须真发生，文本表演过不了
    模板比对。
    """
    # 客户端断开检查：不再发起质检 LLM 调用（断连后停止一切 LLM 开销）
    if _stopped(config):
        logger.info("[reflector] cancelled (client disconnected)")
        raise AgentCancelled()
    plan = parse_plan(state["plan"])
    count = state.get("reflection_count", 0)

    last = state["messages"][-1]
    last_content = (getattr(last, "content", "") or "").strip()

    if plan["chat"]:
        # 闲聊快道声称闸（20260828 影子系统重构，20260831 扩展读取声称）：
        # chat 不再无条件非空豁免——回复含执行声称（已显示/已发送/已执行…）
        # 或读取声称（重读/查过了/读完了全文…）且当前轮无任何工具执行时，
        # 声称没有事实依据（chat 计划 TOOLS 为空，工具调用无合法性），REVISE。
        # 纯文本比对 + 轨迹扫描，不花 LLM 钱；轨迹确有工具执行（越权调用但
        # 事实发生）则放行——声称以工具返回为据。
        # 20260902 工具调用声称族并入：调用了X工具/基于工具返回/点名具体工具名
        guard = _claim_guard_correction(
            last_content, _current_round(state["messages"]))
        if guard:
            issue, corr = guard
            correction = _correction_msg(issue, corr)
            logger.info("[reflector] 声称闸：chat 回复含声称但零工具调用")
            return {"messages": [correction], "done": False,
                    "reflection": "chat 回复含声称但零工具调用", "reflection_count": count + 1,
                    "last_issue": {"issue": issue, "detail": "chat 回复含声称但零工具调用"}}
        return {"done": bool(last_content), "reflection": "chat 快道路：非空检查通过" if last_content else "chat 回复为空", "reflection_count": count}

    # effect/darkmode 幂等判定（确定性计算，注入 LLM 质检上下文 + 检查点 1 豁免
    # 依据；须在预算耗尽分支之前——最后通牒的"非幂等零工具"判定依赖它）：
    # 按上下文 current_effects/current_darkmode 与计划参数计算"状态是否与目标一致"。
    # 背景：LLM 质检会把回复契约的"与目标一致时不调用工具"条款理解反——曾把
    # current=sakura、目标=off（不一致，必须调工具）误判为"一致、调用违规"，
    # 把正确执行 REVISE 掉（帧随 __RESET__ 作废 → golden 缺帧失败）。程序先算好
    # 事实注入质检上下文，两个方向的误判都消除。
    # 注：幂等豁免与判罚（20260902 豁免收窄后）——state_matches=True（状态已与
    # 目标一致）是检查点 1 的合法零调用豁免依据（exempt_idem）；非幂等场景零工具
    # = 偷懒承诺，由检查点 1 确定性 REVISE（multi_turn_correction FAIL 后不再
    # "判罚交给 LLM"；模型合理拒绝仍可经 REVISE→预算耗尽/最后通牒路径以文本收尾）。
    state_matches: bool | None = None
    if plan["skill"] in ("effect", "darkmode") and plan["params"]:
        ctx_text = "\n".join((getattr(m, "content", "") or "")
                             for m in state["messages"] if isinstance(m, HumanMessage))
        if plan["skill"] == "effect":
            eff, act = plan["params"].get("effect"), plan["params"].get("action")
            if eff and act:
                m = re.search(r"current_effects=([^\s,;]+)", ctx_text)
                cur = m.group(1) if m else "none"
                # current_effects 是多特效逗号拼接（sakura,rain…），字符串相等
                # 会把"关 sakura（current=sakura,rain）"误判为"已关"（eff != cur 恒真）
                # ——幂等注记放行零工具，模型偷懒声称完成被 LLM 质检双重放行
                # （multi_turn_correction 20260902 FAIL 根因），必须按集合判断
                cur_effects = {e.strip() for e in cur.split(",") if e.strip()}
                state_matches = (act == "on" and eff in cur_effects) or (
                    act == "off" and eff not in cur_effects)
        else:
            mode = plan["params"].get("mode")
            if mode:
                m = re.search(r"current_darkmode=([^\s,;]+)", ctx_text)
                cur = m.group(1) if m else "off"
                state_matches = (mode == "on" and cur == "on") or (mode == "off" and cur == "off")

    if count >= MAX_REFLECTIONS:
        # 预算耗尽 → 接受当前结果收尾：纠错循环必须有上限，不无限烧钱/烧时间。
        # 20260831 硬拦：工具执行缺失是硬约束，不受反思预算约束——预算耗尽仍零
        # 工具调用时不得 accept（golden rag_talk_rag 实证：REVISE 两次后模型编
        # "已读取全文确认"，预算耗尽 accept 放行、LLM 质检被声称型回复骗过）。
        # 追加一次最后通牒轮（count 再 +1；下次进来 count == MAX_REFLECTIONS+1
        # 不再进通牒分支 → accept，通牒恰好一轮不无限循环）。
        # 20260902 逐工具核验（与检查点 1 同语义）：预算耗尽仍缺失 TOOLS 行
        # 任一工具 → 最后通牒（如双源只调了 list_guestbook 仍缺 list_talks）
        missing = (plan["tools"]
                   and not (plan["skill"] in ("effect", "darkmode")
                            and state_matches is True)
                   and _missing_tools(plan["tools"], _current_round(state["messages"])))
        if missing and count < MAX_REFLECTIONS + 1:
            correction = _correction_msg(
                "tool_missing_final",
                "质检打回已达上限，但你仍然没有调用计划列出的工具（"
                + "、".join(plan["tools"])
                + "），本轮缺失："
                + "、".join(missing)
                + "；这是最后一次机会：先调用工具、基于工具返回的事实回答；"
                "仍然直接作答将被视为编造",
            )
            logger.info("[reflector] 预算耗尽仍零工具调用，最后通牒")
            return {"messages": [correction], "done": False,
                    "reflection": "预算耗尽仍零工具调用（最后通牒）",
                    "reflection_count": count + 1,
                    "last_issue": {"issue": "tool_missing_final", "detail": "预算耗尽仍零工具调用"}}
        if missing:
            # 最后通牒轮仍零调用 → 接受收尾，但记录 issue（trace 可见，归因时
            # 能区分"给过最后机会仍拒绝"与"闸门没拦"）
            logger.warning("[reflector] 最后通牒后仍零工具调用，接受（accepted_missing_tools）")
            return {"done": True,
                    "reflection": "预算耗尽且零工具调用（已发最后通牒），接受当前结果",
                    "reflection_count": count,
                    "last_issue": {"issue": "accepted_missing_tools", "detail": "最后通牒后仍零工具调用"}}
        logger.info("[reflector] 反思预算耗尽(%d/%d)，接受当前结果", count, MAX_REFLECTIONS)
        return {"done": True, "reflection": f"反思预算已用尽({count}/{MAX_REFLECTIONS})，接受当前结果", "reflection_count": count}

    # 模板执行的结构性检查（检查点 1 的确定性部分，LLM 质检前的低成本闸）：
    # 20260831 泛化：不再限于 navigate——凡 TOOLS 行要求工具但当前轮从未执行过
    # 任何工具，正文直接回答没有事实依据，REVISE，不花 LLM 钱。
    # 20260902 升级逐工具核验：不只查"有没有任何 ToolMessage"，而是 TOOLS 行
    # 每个工具名都要在轨迹中出现（_missing_tools）——planner 显式点名双源
    # （list_guestbook+list_talks）后只调一个数据源仍算执行不完整。
    # 事故依据：关 thinking 后 executor 在 rag_query 技能下不调工具直接编造回复
    # （"分析文章21"编出文章不存在的文献 Poldrack 2015/Farah 2004 与发布日期），
    # LLM 质检被声称型回复骗过放行 PASS；20260902 233815 模型零工具声称"两边都
    # 翻了"（planner 点名工具进 TOOLS 行后，本闸门直接拦截）——确定性闸门先于
    # LLM 质检。
    # 检查范围限定当前轮（_current_round）而非全历史：被 REVISE 的历史轮调用过
    # 工具不代表当前轮也调了。
    # 豁免面（合法零调用，不误伤）：
    #   - NOTE 行明示"不调用任何工具"（导航已下线/不存在/未识别）→ 计划 TOOLS
    #     已为空，本闸门不触发（下方另有反向检查兜住越权调用）
    #   - effect/darkmode 幂等场景（状态已与目标一致时合法零调用）→ 走上方
    #     state_matches 注记 + LLM 检查点 5 判罚，确定性闸门不越权
    round_msgs = _current_round(state["messages"])
    missing_tools = _missing_tools(plan["tools"], round_msgs)
    # effect/darkmode 豁免收窄（20260902）：不再是"整个技能豁免"，只豁免
    # 幂等场景（state_matches=True，状态已与目标一致 → 合法零调用）——非幂等
    # 场景（目标与当前状态不一致）零工具 = 偷懒承诺/声称完成，必须确定性 REVISE。
    # 依据：multi_turn_correction FAIL——current=sakura,rain 目标关 sakura，
    # 模型零工具回复"帮你把樱花关掉"，LLM 质检要点 1 明确不判工具缺失、
    # 要点 5 只拦完成式声称（"帮你关"是将来时），三层放行零工具缺帧。
    exempt_idem = plan["skill"] in ("effect", "darkmode") and state_matches is True
    if (plan["tools"] and not exempt_idem and missing_tools):
        correction = _correction_msg(
            "tool_missing",
            "计划要求调用工具（" + "、".join(plan["tools"]) + "），本轮缺失："
            + "、".join(missing_tools)
            + "；回答必须基于工具返回的事实——先调用缺失的工具再作答，不得编造内容；"
            "若因访客明确禁止等原因无法调用工具，如实告知无法执行",
        )
        logger.info("[reflector] 模板执行缺失：%s 计划要求工具，缺失 %s",
                    plan["skill"], "、".join(missing_tools))
        return {"messages": [correction], "done": False,
                "reflection": f"{plan['skill']} 计划要求工具，缺失 {'、'.join(missing_tools)}",
                "reflection_count": count + 1,
                "last_issue": {"issue": "tool_missing", "detail": f"计划要求工具，缺失 {'、'.join(missing_tools)}"}}

    # 20260901：留言板/说说类查询的候选读取校验已删除——检索池只收文章后
    # rag_search 不再返回 talk/board 候选，该检查触发条件消失；留言/说说查询
    # 改走 list_guestbook/list_talks 数据工具（content_query 自由模式），无需检索。

    # URL/资源路径声称校验（确定性，LLM 质检前，20260901）：回复中的资源 URL
    # （/api/ 路径或图片后缀 URL）必须逐字出现在工具返回或用户消息中——URL 是
    # 机器串，模型不会改写，逐字校验无假阴性。双向收益：① 拦截编造 URL（1.29
    # "凭空造图"：模型编 /api/xxx.jpg 回复）② 放行真实 URL（20260901"测试4封面"
    # 事故：search_notes/get_article_detail 返回真实 cover 被反射器误杀打回——
    # 那是 _build_trace 截断导致的信息缺失，URL 逐字比对不受影响）。代码块内
    # URL 不校验（教程/示例场景）；裸域名引用（saudade.site）不提取（非资源声称）。
    round_msgs = _current_round(state["messages"])
    last_ai = next((m for m in reversed(round_msgs) if isinstance(m, AIMessage)), None)
    reply = (getattr(last_ai, "content", "") or "") if last_ai else ""
    if reply:
        code_stripped = re.sub(r"```.*?```", "", reply, flags=re.S)
        # 排除集含 markdown 格式符（` 代码块/反引号、* 强调、|）与全角括号——
        # 否则 `url` 反引号包裹时右反引号被吞进匹配串，逐字比对误杀真实 URL
        # （20260901"测试4封面"验证实证：回复以 `代码块` 形式给 cover URL
        # 被检查点 1.6 误判"不存在的 URL"）
        urls = re.findall(
            r"/api/[^\s)\]\"'<>，。、；：`*|）]+|https?://[^\s)\]\"'<>，。、；：`*|）]+\.(?:jpe?g|png|webp|gif|svg)",
            code_stripped)
        if urls:
            trusted = "\n".join(_msg_text(m) for m in state["messages"]
                                if isinstance(m, (HumanMessage, ToolMessage)))
            # 绝对 URL 归一化（20260901）：模型可能把相对路径写成
            # https://saudade.site/api/...jpg（带域名）——逐字比对会误杀真实 URL，
            # 取 path 部分（https://host/x → /x）与工具返回的相对路径比对。
            def _url_ok(u: str) -> bool:
                if u in trusted:
                    return True
                m = re.match(r"https?://[^/]+(/.*)$", u)
                return bool(m and m.group(1) in trusted)
            missing = [u for u in urls if not _url_ok(u)]
            if missing:
                correction = _correction_msg(
                    "fabricated_url",
                    "回复中包含的资源 URL（" + "、".join(missing[:3]) + "）在工具返回与"
                    "用户消息中都不存在——图片/资源链接必须来自工具返回的真实 URL"
                    "（如 get_article_detail / search_notes 返回的 cover 字段），"
                    "不得编造；需要图片链接时先检索再如实报告",
                )
                logger.info("[reflector] URL 声称无依据：%s", "、".join(missing[:3]))
                return {"messages": [correction], "done": False,
                        "reflection": "回复含工具返回与用户消息中不存在的 URL",
                        "reflection_count": count + 1,
                        "last_issue": {"issue": "fabricated_url",
                                       "detail": "回复含工具返回中不存在的 URL"}}

    # 确认式导航声称检查（确定性，LLM 质检前）：navigate 当前轮工具已调用，
    # 但轨迹中只有 NAVIGATE:（待确认）帧、无 AUTO_NAVIGATE: 帧时，回复不得含
    # 完成式声称词——NAVIGATE: 是"请求确认"，不是"已跳转"。曾见模型调用工具
    # 返回 NAVIGATE: 后回复"已经带您到文章页"（前端确认框未确认、页面没动），
    # 用户视角即幻觉。检查范围限定当前轮，仅文本比对不花 LLM 钱。
    if plan["skill"] == "navigate" and plan["tools"]:
        round_msgs = _current_round(state["messages"])
        tool_text = "\n".join(
            (getattr(m, "content", "") or "") for m in round_msgs if isinstance(m, ToolMessage)
        )
        if "NAVIGATE:" in tool_text and "AUTO_NAVIGATE:" not in tool_text:
            last_ai = next(
                (m for m in reversed(round_msgs) if isinstance(m, AIMessage)), None
            )
            reply = (getattr(last_ai, "content", "") or "") if last_ai else ""
            if re.search(r"(已经?带|已经?到|已经?跳转|跳转成功|成功[^\n。，,]*?(跳|转)|过去了|已经?去)", reply):
                correction = _correction_msg(
                    "navigate_confirm_claim",
                    "navigate 工具返回的是 NAVIGATE: 确认式帧——页面正在等待访客确认，尚未跳转；"
                    "不得声称已到达/已跳转，改为如实说明'已为您打开跳转确认'并请访客确认；"
                    "如需直接跳转，重新调用 navigate_to 且 confirm=false（返回 AUTO_NAVIGATE: 帧）后再确认到达",
                )
                logger.info("[reflector] 确认式导航声称：NAVIGATE 帧 + 完成式声称")
                return {"messages": [correction], "done": False,
                        "reflection": "确认式导航但声称已到达", "reflection_count": count + 1,
                        "last_issue": {"issue": "navigate_confirm_claim", "detail": "NAVIGATE 帧 + 完成式声称"}}

    # 设备显示结构性检查（确定性，LLM 质检前）：device_display 计划要求调用
    # device_oled_display 但当前轮零工具执行 → 声称"已显示/已发送/已刷新"无依据，
    # REVISE。曾见（20260827 实测）：模型不调工具、回复声称"正在向屏幕发送指令"
    # ——文本表演过不了此检查。影子系统（force_display 注记快道）已删除，此处是
    # 显示声称的唯一确定性闸口；检查范围限定当前轮（同 navigate 检查）。
    if (plan["skill"] == "device_display"
            and plan["tools"]
            and not any(isinstance(m, ToolMessage) for m in _current_round(state["messages"]))):
        correction = _correction_msg(
            "device_tool_missing",
            "计划要求调用 device_oled_display 工具，但本轮没有任何工具执行；"
            "立即调用 device_oled_display 下发显示内容（text 为你要显示的文字，不能是访客指令原文），"
            "工具返回后再按结果回复；设备离线/下发失败时如实告知，不得声称已显示/已发送/已刷新屏幕",
        )
        logger.info("[reflector] 设备显示缺失：device_display 计划零工具调用")
        return {"messages": [correction], "done": False,
                "reflection": "device_display 计划要求工具但零工具调用", "reflection_count": count + 1,
                "last_issue": {"issue": "device_tool_missing", "detail": "device_display 计划要求工具但零工具调用"}}

    # 反向结构性检查：计划 NOTE 行明示"不调用任何工具"（友链下线/页面不存在等，
    # 此时 TOOLS 为空），但模型仍越权调用了工具（如主动跳转留言板）→ 违反模板
    # 契约，REVISE。此检查只对带该标记的计划生效，不影响 content_query 等自由用
    # 工具的技能。同样限定当前轮（历史轮已作废，不构成越权）。
    if ("不调用任何工具" in plan["note"]
            and any(isinstance(m, ToolMessage) for m in _current_round(state["messages"]))):
        correction = _correction_msg(
            "note_violation",
            "计划要求不调用任何工具（如实告知即可），但本轮调用了工具；"
            "不要调用任何工具，直接在文本中如实告知（可给出其他页面的 Markdown 链接作为建议）",
        )
        logger.info("[reflector] 模板越权：NOTE 要求零工具但调用了工具")
        return {"messages": [correction], "done": False,
                "reflection": "NOTE 要求零工具但调用了工具", "reflection_count": count + 1,
                "last_issue": {"issue": "note_violation", "detail": "NOTE 要求零工具但调用了工具"}}

    # 零工具注记确定性闸（LLM 质检前）：计划 NOTE 明示"不调用任何工具"
    # （友链下线/页面不存在/无法识别），当前轮零工具调用（反向检查已拦越权）——
    # 模板契约 = 如实告知，把 LLM 质检的"是否如实告知"检查点确定性化：
    # 回复含如实措辞（下线/不存在类关键词）→ PASS（golden 实测 LLM 质检此场景
    # 每次白等约 30s 后兜底 PASS，与确定性判定等价但慢 6 倍）；措辞不实
    # （如声称"已跳转"）→ REVISE 要求如实告知，不花 LLM 钱。
    if ("不调用任何工具" in plan["note"]
            and not any(isinstance(m, ToolMessage) for m in _current_round(state["messages"]))):
        round_msgs = _current_round(state["messages"])
        last_ai = next(
            (m for m in reversed(round_msgs) if isinstance(m, AIMessage)), None
        )
        reply = (getattr(last_ai, "content", "") or "").strip() if last_ai else ""
        if reply:
            if "已下线" in plan["note"]:
                honest = any(k in reply for k in ("下线", "下架", "无法访问", "没有了"))
            else:
                honest = any(k in reply for k in ("没有", "不存在", "找不到", "无法识别", "没有找到"))
            if honest:
                logger.info("[reflector] 零工具注记 + 如实措辞 → 确定性 PASS（跳过 LLM 质检）")
                return {"done": True, "reflection": "确定性 PASS：NOTE 要求零工具且已如实告知", "reflection_count": count}
            correction = _correction_msg(
                "not_honest",
                "计划要求如实告知目标页面不存在/已下线（不调用任何工具），但回复没有如实说明；"
                "不要调用任何工具，在回复中如实说明该页面不存在或已下线"
                "（例如'该页面不存在/已下线'），可附其他真实页面的文本链接作为建议",
            )
            logger.info("[reflector] 零工具注记但未如实告知 → REVISE")
            return {"messages": [correction], "done": False,
                    "reflection": "NOTE 零工具但未如实告知", "reflection_count": count + 1,
                    "last_issue": {"issue": "not_honest", "detail": "NOTE 零工具但未如实告知"}}

    # 工具帧确定性闸放行（LLM 质检前）：工具型技能当前轮已有成功工具帧产出
    # （导航 AUTO_NAVIGATE/NAVIGATE、特效 EFFECT、暗色 DARKMODE、设备类非错误返回）
    # 且最终回复非空 → 直接 PASS，不花 LLM 钱。实测 qwen 质检调用频繁挂起/超时
    # （DB 实证：导航任务总耗时 39s/50s，其中约 30s 耗在质检超时兜底）——用户
    # "规划10几秒/卡一会"的根因。质检是防幻觉增强，非流程必需：工具帧产出 =
    # 动作真实发生（防幻觉核心不变量已满足），上方模板结构性检查与确认式声称
    # 检查已确定性拦下"零工具声称/确认式帧+完成式声称"等违规形态；闸放行后
    # 再被 REVISE 的只剩设备类失败与模型自由发挥的文案问题，交给 LLM 质检兜底。
    if (plan["skill"] in ("navigate", "effect", "darkmode", "device_display", "device_query")
            and plan["tools"]):
        round_msgs = _current_round(state["messages"])
        tool_text = "\n".join(
            (getattr(m, "content", "") or "") for m in round_msgs if isinstance(m, ToolMessage)
        )
        last_ai = next(
            (m for m in reversed(round_msgs) if isinstance(m, AIMessage)), None
        )
        reply = (getattr(last_ai, "content", "") or "").strip() if last_ai else ""
        if tool_text and reply:
            ok = any(f in tool_text for f in ("AUTO_NAVIGATE:", "NAVIGATE:", "EFFECT:", "DARKMODE:"))
            if not ok and plan["skill"] in ("device_display", "device_query"):
                # 设备类无帧前缀：非错误返回即成功（失败文本均含特征词）
                ok = not any(bad in tool_text for bad in (
                    "失败", "无效", "无法", "为空", "非法", "不存在", "异常",
                    "离线", "未绑定", "未下发", "错误", "ERROR"))
            if ok:
                logger.info("[reflector] 工具帧已产出 + 回复非空 → 确定性 PASS（跳过 LLM 质检）")
                return {"done": True, "reflection": f"确定性 PASS：{plan['skill']} 工具帧已产出", "reflection_count": count}

    # 声称闸 content_query 分支（20260902，134537 实证）：自由 ReAct 的 TOOLS 行
    # 为空 → 检查点 1 空转，LLM 质检被"用 rag_search 搜了一遍"型声称骗过放行
    # （13:45 零调用 PASS、质检自述"调用了 rag_search 和 list_talks 工具"——质检
    # 采信了模型自己的声称）。零工具轮三类声称与 chat 同正则、同 helper 确定性
    # 打回，不花 LLM 钱；executor 规则 1 已要求"涉及博客数据必须调工具核实"，
    # 本闸补执行端兜底。
    if plan["skill"] == "content_query":
        guard = _claim_guard_correction(
            last_content, _current_round(state["messages"]))
        if guard:
            issue, corr = guard
            correction = _correction_msg(issue, corr)
            logger.info("[reflector] 声称闸：content_query 回复含声称但零工具调用")
            return {"messages": [correction], "done": False,
                    "reflection": "content_query 回复含声称但零工具调用",
                    "reflection_count": count + 1,
                    "last_issue": {"issue": issue, "detail": "content_query 回复含声称但零工具调用"}}

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
    # 历史轮已调用工具名清单（20260901）：轨迹按轮次裁剪后 LLM 质检看不到上一轮
    # 已调过的工具——"第一轮已 rag_search、第二轮续接 get_article_detail 读取"
    # 被误判为"未执行计划要求的 rag_search"。只列工具名、不展示历史轮内容：
    # 防豁免（检查点 1 仍按当前轮判缺失）与防误判（续接读取可见）兼得。
    past_tools = sorted({getattr(m, "name", "") for m in state["messages"]
                         if isinstance(m, ToolMessage)})
    tools_note = (
        f"历史轮已调用工具（供判断续接读取是否合理，内容不参与判罚）: "
        f"{'、'.join(past_tools)}\n" if past_tools else "")
    # 本轮实际执行工具记录注记（20260902，134537 实证：content_query 零调用被
    # 质检放行，质检自述"调用了 rag_search 和 list_talks 工具"——它采信了回复
    # 里的自称而不是轨迹。程序先算好事实注入，声称与记录的比对不容模型自由发挥。
    # 只列当前轮（_current_round 口径与轨迹同源），历史轮见上方 tools_note。
    cur_tools = sorted({getattr(m, "name", "") for m in cur
                        if isinstance(m, ToolMessage)})
    if cur_tools:
        tools_note += (
            "本轮实际执行工具记录（回复声称调用的工具必须在此列表中有对应记录）: "
            f"{'、'.join(cur_tools)}\n")
    else:
        tools_note += (
            "本轮实际执行工具记录: 无。\n"
            "回复若声称调用了工具（「我调用了X」「用X搜了一遍」「查了/翻了X」），"
            "与本记录不符即编造，判 REVISE——未调用工具时如实说明尚未查看，"
            "不要声称调用过。\n")
    # 质检异常兜底：LLM API 抖动/超时不应杀死整个对话（实测：反射调用挂起/抛错 →
    # 流中断 → 前端 catch 不执行导航 → 用户"卡死"且命令帧白发）。质检是防幻觉增强，
    # 非流程必需：工具帧已产出（导航命令已发出）时异常即放行，让流正常收尾。
    try:
        resp = llm.invoke(_REFLECTOR_PROMPT.format(
            plan=state["plan"], trace=trace, tools_note=tools_note, idem_note=idem_note))
    except Exception as e:
        logger.warning("[reflector] LLM 质检异常，兜底 PASS（流正常收尾，工具帧不受影响）: %s", e)
        return {"done": True, "reflection": f"LLM 质检异常兜底 PASS: {e}", "reflection_count": count}
    raw = getattr(resp, "content", str(resp))
    if re.search(r"VERDICT\s*[:=]\s*REVISE", raw, re.IGNORECASE):
        m = re.search(r"NOTE\s*[:=]\s*(.+)", raw, re.IGNORECASE)
        note = m.group(1).strip() if m else "未按计划执行"
        correction = _correction_msg("llm_revise", note)
        logger.info("[reflector] REVISE: %s", note)
        return {"messages": [correction], "done": False, "reflection": note,
                "reflection_count": count + 1,
                "last_issue": {"issue": "llm_revise", "detail": note}}

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
    return {"messages": messages, "plan": "", "reflection": "", "reflection_count": 0,
            "done": False, "tool_retries": [], "last_issue": None}
