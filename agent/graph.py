"""手写 LangGraph 图 —— Agent 核心重写（20260903 架构裁决：planner 全权）

替代 create_agent 黑盒：显式声明 planner / execute / model / gate 节点与状态流转。
面试核心：能讲清楚"你的 agent 循环怎么设计"——
  1. 状态 schema 为什么这么设计（工作台上放什么数据）
  2. 节点分工（每个工人干什么）
  3. 条件边（什么情况下循环、终止、纠错）

架构（20260903 定稿，废除自由 ReAct）——确定性骨架 + 单一决策点：
  * 决策点只有一个：planner（fast paths 是确定性快道，不是第二决策者）。
    planner 产出调用清单（PARAMS.tools / PARAMS.calls，白名单校验），
    工具与参数在 planner 这一侧全部决定。
  * execute 节点零自由：按 planner 调用清单逐条确定性执行（literal_eval
    参数 → _TOOL_MAP），产出 ToolMessage 帧 → 回 planner 看结果再决策。
  * model 节点零工具（不 bind_tools，结构上不可能发出 tool_calls）：
    只当 narrator——基于工具帧 + 页面上下文 + 叙述纪律组织最终回复。
  * gate 节点是唯一确定性检查（取代原 reflector 的 9 个确定性闸 + LLM 质检）：
    检查不通过没有 REVISE 循环——validate → fallback 文本直接收尾
    （fallback 是给访客看的如实回复，不再是"修正要求"）。
  * 不存在 REVISE / LLM-QC / 反思预算 / 最后通牒 / 工具重试状态机。

20260904 裁决（回执驱动，架构重反思后加回"受阻复盘"而非"叙述质检"）：
  * execute 循环内逐 spec 做 checker 确定性验收（_check_spec：错误帧/空结果/
    命令形态），PASS → receipts 回执（系统确认的事实，跨轮执行记忆的原料），
    BLOCK → blocked 受阻清单。
  * 受阻首现 → 回 planner 改参重试（rule5，零新增 LLM）；同 spec 二次受阻
    （blocked_repeat）→ reflector 复盘节点（≤2 次，输入=计划+受阻+回执+帧，
    结构性无叙述文本），产出 ISSUE 交 planner 重规划或 wrap_up 确定性终局。
  * 老 reflector 死于"LLM 读散文做质检"（1.26/1.30/1.32 事故）；本 reflector
    只分析确定性受阻数据——叙述质检、REVISE 打回 model 不复活。

改动背景（用户裁决，见问题记录 20260903）：三次事故（声称闸词表被绕、
LLM-QC 采信模型自称、预算耗尽 accept）共同指向一个根因——执行器自由度
太高：参数自拟（planner 说 /about 执行器篡成 /article/15）、调用与否自决
（TOOLS 行点名仍可零调用）、输出权自握（REVISE 打回可忽略、预算耗尽仍收）。
修复不是再补一层检查（事后找补），而是把自由度从执行层全部收走：执行层
变成确定性执行器后，"不听话"在结构上不可能发生——检查层随之可以大幅
简化（gate 只兜模型叙述层的文本失真）。

LangGraph 四件套（对照第一课讲解）：
  State  —— AgentState（节点间共享的字典，字段决定"工作台长什么样"）
  Node   —— planner/execute/model/gate（每个是普通函数：state 进、更新字段出）
  Edge   —— 普通边（顺序传送带）+ 条件边（按返回值路由，循环/终止所在）
  Reducer—— Annotated[list, add_messages]：messages 字段"追加"而非覆盖

与现有工程外壳的关系（全部保留不动）：
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
from agent.prompts import BLOG_ASSISTANT_PROMPT, STICKER_GUIDE
from agent.skills import (FUZZY_NAV_RULES, NAV_MAP, SKILL_MAP,
                          build_planner_context, instantiate_plan)
from utils.trace import record

logger = logging.getLogger(__name__)

# 客户端断开（stop_event 置位）→ 图内节点主动终止执行。
# 场景（20260827 实测）：浏览器连接中断后 event_stream 无法及时感知（卡在
# queue.get），agent 线程无感知继续执行 ReAct 循环——曾见断连后仍执行
# device_oled_display 写操作。server.py 侧 2s 轮询断连 → set stop_event →
# 图内各节点在"下一次执行前"检查并抛此异常终止，写操作绝不发生在用户已离开
# 之后。由 server.py 捕获（静默收尾，客户端已断无帧可发）。
class AgentCancelled(Exception):
    pass


def _stopped(config: RunnableConfig | None) -> bool:
    """节点级中断检查：stop_event（threading.Event）由 server.py 经 config 注入。"""
    ev = (config or {}).get("configurable", {}).get("stop_event")
    return ev is not None and ev.is_set()


# 工具一次构建全局复用（tools/base.py 的 @tool 都是纯函数，无状态）
_TOOLS = get_all_tools()
_TOOL_MAP = {t.name: t for t in _TOOLS}

# 规划轮次上限：planner ⇄ execute 循环最多决策 MAX_PLAN_ROUNDS 轮，之后强制
# 收尾（基于已有工具返回如实作答）。防止 planner LLM 无限追问/重复调用烧钱。
# 每轮 = planner 一次决策；收尾轮（计划 TOOLS 为空）直接走 model，不占用。
MAX_PLAN_ROUNDS = 4

# 复盘轮次上限（20260904）：同 spec 二次受阻（rule5 首轮改参重试已败/链断）才
# 进 reflector——罕见异常路径，LLM 复盘 ≤ REFLECT_MAX_ROUNDS 次，到顶确定性
# 终局收尾（无静默 accept）。老 reflector 的教训：复盘必须小预算，LLM 循环是
# 死亡螺旋的燃料（1.26/1.30/1.33 事故均在长复盘链上）。
REFLECT_MAX_ROUNDS = 2


# ---------------------------------------------------------------------------
# 1. State：节点间共享的"工作台"
# ---------------------------------------------------------------------------

class AgentState(TypedDict):
    """图状态。planner 写计划，execute 确定性执行，model 叙述，gate 检查收尾。

    - messages:    对话消息（用户问题/工具帧/叙述回复的流水）。Reducer=
                   add_messages 表示"追加"——这正是 create_agent 里消息只增
                   不减的机制，我们显式声明出来。
    - plan:        planner 本轮决策的计划（契约文本，见 parse_plan/plan_encode）。
    - plan_rounds: 已决策轮数（上限 MAX_PLAN_ROUNDS，防 planner⇄execute 死循环）。
    - done:        gate 检查完置 True → 边路由到 END。
    - executed:    execute 已执行的调用清单 spec（原文去重）——planner 拦
                   "检索原句重复发"的轮次浪费用（20260903 golden 实证）。
    - receipts:    checker 验收 PASS 的累计回执（请求内累计，与 executed 同
                   模式）——系统确认过的执行事实 [{skill,tool,args,result,ts}]，
                   是 reflector 输入与跨轮执行记忆（__EXEC__ 帧）的原料。
    - blocked:     本轮 execute 的 BLOCK 受阻项（[{spec,tool,reason,result}]，
                   只含本轮——路由判断与 reflector 输入用）。
    - blocked_seen: 请求内累计受阻 spec 原文（blocked 的累计集，repeat 判定用）。
    - blocked_repeat: 本轮受阻项里是否有此前已受阻过的 spec（= 首轮改参重试
                   已失败/链断）→ 路由去 reflector。
    - reflect_rounds: reflector 复盘次数（≤ REFLECT_MAX_ROUNDS，到顶确定性终局）。
    - issues:       reflector 上次输出的 ISSUE 文本（注入下一轮 planner 提示词）。
    - reflect_end:  reflector 判定终局（wrap_up/预算耗尽）→ 路由去 model 叙述。
    """

    messages: Annotated[list, add_messages]
    plan: str
    plan_rounds: int
    done: bool
    executed: list[str]
    receipts: list[dict]
    blocked: list[dict]
    blocked_seen: list[str]
    blocked_repeat: bool
    reflect_rounds: int
    issues: str
    reflect_end: bool


# ---------------------------------------------------------------------------
# 2. 模块间契约：planner 写入 plan 字段，execute/model/gate 读取
# ---------------------------------------------------------------------------
# plan 字段 = 技能模板实例化后的计划文本（受限规划——planner 只从技能注册表
# agent/skills.py 选技能 + 填参数，不自由写步骤）：
#   第 1 行: SKILL=<技能名>（navigate/effect/darkmode/device_display/
#            device_query/content_query/chat/read_article）
#   第 2 行: PARAMS=<JSON 参数>（如 {"target": "物联网平台", "mode": "direct"}）
#   第 3 行: TOOLS: <实例化后的工具调用序列>（chat/收尾轮为"（无）"）
#   第 4 行: NOTE: <业务注记>（导航目标下线/不存在/已按决策执行等）
#   第 5 行: REPLY: <技能回复契约>（model 叙述时遵守、gate 不做文本级对照）
#   第 6 行: TODO: <剩余步骤列表>（可选，多步链中间轮声明：后续依赖步骤用 →
#            分隔。20260904 起：TODO 是"声明"不是"执行指令"——execute 只执行
#            TOOLS 行，TODO 供 reflector 判链依赖/checker 语境/trace 留痕，
#            不进执行路径；依赖步骤的参数只能等上轮工具返回后填，不预编）
# 导航映射表在 skills.py（页面别名→路径，"物联网平台→/device-console/"是系统数据，
# 不是模型猜测）——planner 跑题的结构性根因（不知道工具语义）由此消除。
# 20260903：TOOLS 行不再是"允许名单"而是"执行清单"——execute 节点把它当命令
# 逐条执行，不存在"TOOLS 点了名仍可不调用"的自由（旧架构的自由空间之一）。

_PLANNER_PROMPT = """\
你是博客客服 Agent 的规划器——本架构中唯一的决策者。执行层没有自由意志：
系统会把你本轮调用清单里的工具逐条确定性执行，然后把工具返回带回到你这里，
由你决定下一步。你负责：选技能、填参数、决定每轮执行哪些工具、判断何时
信息已足够收尾。

技能注册表（唯一可选集合，禁止自创步骤或自由发挥，一次选一个）：
{skills_context}

本轮可规划执行的查询工具（知识型问题在 PARAMS.calls 里点名，必须带真实参数）：
{tools_desc}

当前页面上下文（前端实时上报的事实——访客当前位置/特效/夜间模式以此为准，
不要凭对话历史推断位置）：
{page_ctx}

{round_info}

{recent_context}

{tool_results}

复盘建议（reflector 对重复受阻项的 ISSUE 修正指引——仅当上一轮复盘判 replan
后才有内容；没有则为缺省语，按常规规则决策）：
{reflector_feedback}

判定规则：
1. 决策类型（SKILL）：
   - chat：纯闲聊/问候/情感/通用知识——与博客任何内容（文章/说说/留言/公告/
     站点信息/功能页面）无关时才用。
   - content_query：一切与博客内容有关的询问与核实（文章/说说/留言/公告/站点
     信息里写了什么、怎么做、是什么；博客机制如何工作，如"agent 怎么防止模型
     假装调用了工具"；页面/内容存在性质疑，如"真有这个页面？确定有这篇？"——
     注意质疑"某操作是否真执行过"不是本技能，见规则 6 的 recent_executions）。
   - navigate/effect/darkmode/device_display/device_query：对应动作技能
     （用户要求去某页/开特效/切夜间模式/屏幕上显示文字/查设备）。
2. 涉站必查：问题只要可能涉及站内内容就选 content_query 并给调用清单，不得
   退化成 chat 凭印象答——答案在博客内容里，不在你的记忆里。
3. content_query 每轮都必须给调用清单（calls 或 tools），只允许两种情况留空
   （收尾轮）：①已有工具返回、信息足够；②工具返回明确查无结果。规划方式：
   - 列表/数据型（最新留言/说说/公告、时间等）→ PARAMS.tools 点名无参只读工具；
     问"有没有人聊过/写过 X"必须成对点名 list_guestbook 与 list_talks 两个数据源
   - "有没有/有哪些 X 相关文章"（主题列举）→ PARAMS.calls 必须成对点名两条：
     search_notes(核心词) + list_notes（page=1、page_size=50）——关键词搜
     正文 + 全量标题比对互补，缺一不可（正文措辞常与主题词不一致：问"嵌入式
     相关文章"，搜"嵌入式"只命中 Git 教程一处举例，真相关的是《ESP32-S3-OBC
     固件接入参考》《IoT 设备接入物联网平台指南》，标题含专名而关键词不含；
     只 search_notes 命中不足就收尾、或只 list_notes 不真检索，都不对）；
     两份返回交叉比对后再下"有/没有"的结论
   - 知识型/验证型 → PARAMS.calls 给定位调用。定位工具选型：
     机制/原理/做法型问题（"怎么实现/怎么工作/原理/机制/怎么做到/区别"）先
     rag_search 发用户原句语义定位——关键词 LIKE 会只命中标题含目标词的
     "问题记录"类文章（问"OTA 升级怎么实现"，语义检索第一是《ESP32-S3-OBC
     固件接入参考》OTA 章节，关键词却先命中《ESP32-S3 OTA 问题与解决记录》
     踩坑史）；事实/检索型（要数值/存在性/列举）→ search_notes 关键词：
     [{{"tool": "search_notes", "args": {{"keyword": "<用户原词或最小核心词>"}}}}]
     ——关键词=消息的信息核心词：剥掉称呼/问候/助词（例："小猫咪有没有嵌入式
     相关文章" → 关键词"嵌入式"，绝不是"小猫咪"），宁短勿长
     候选命中后下一轮 get_article_detail 读全文——article_id 只能取上一轮工具
     返回里的真实 id，绝不自己编 id。机制型候选多篇时优先读「参考/接入/指南/
     实现」类文档；「问题与解决记录/踩坑/FAQ」类是经验记录，仅当确实记载所问
     事实时引用。检索零结果应变（至多补一轮）：
     a) 换 rag_search 语义检索一次；仍无 → b) 关键词换用户原词的变体再
     search_notes 一次（中文词零结果时保留数字/字母试原文，如"测试4"→"test4"；
     去掉口语缀词）；仍无 → c) 收尾如实告知"站内没有找到"，不得用记忆硬答
   - 一轮只给当前步，execute 只执行 TOOLS 行。若本计划是多步链的中间一步
     （后续步骤依赖本轮结果：先检索定位 → 下一轮读候选全文；先 content_query
     找到文章 → 下一轮 navigate 跳转），在计划里追加一行 TODO: <剩余步骤列表，
     用 → 分隔>——只描述后续依赖链，不重复本轮已给的步骤；后续步骤的参数
     （article_id 等）只能等上一轮工具返回后填写，绝不预先编造。单步/收尾轮
     不写 TODO 行。
4. 动作技能参数纪律：
   - navigate：target 只能填导航映射表里的别名，或用户消息里以 / 开头的字面
     路径（原样照抄，不改写、不推断成别的页面）；路径是否有效由系统白名单
     校验，无效时系统会给注记，你收尾如实告知即可。
     用户要"去/打开/带我去 XX 文章"：上一轮工具帧/页面上下文里有该文章真实
     id（get_article_detail/search_notes/list_notes 返回的 noteKey）→ target
     填字面路径 /article/<真实id>（如 /article/19，navigate 白名单放行 /article/*）；
     id 只取帧内真实存在值，绝不编造。id 不在可见帧 → 先 content_query 定位
     （规则 3），拿到真实 id 后下一轮再 navigate。⚠ 用户明确要去某篇文章时，
     禁止拿首页或其他页面兜底执行——决议不出目标就如实说明或先给文章链接
   - effect/darkmode：先看页面上下文 current_effects/current_darkmode——状态
     已与用户要求一致时【不要调用工具】，选 chat 直接把现状告诉访客
     （幂等：零调用是正确行为）；不一致才规划 toggle_effect/toggle_dark_mode。
     "把X换成/改成/不要X要Y"（X 当前开着、Y 是目标特效）＝**两个状态变更**：
     同一 TOOLS 行给两条 spec（X off + Y on），execute 逐条执行——只关 X 不
     开 Y 等于没完成"换成 Y"，目标效果必须真的开启
   - device_display：不填 text 参数（屏幕文案由系统在展示时结合对话创作）
5. 多轮收敛：
   - 已执行动作技能（navigate/effect/darkmode/device_display/device_query/
     read_article）且工具返回已可见 → 本轮收尾（chat 或 content_query 留空），
     绝不重复规划同款调用——动作已由工具帧完成，回复层会基于帧确认
   - 上一轮工具返回以 __ERROR__ 开头 → 按错误修正参数重试一次；已重试过或
     无法修正 → 收尾如实告知失败，不得声称成功
   - 上方复盘建议存在（reflector ISSUE，指明受阻项缺什么/怎么改）→ 按建议
     重试该修正；按建议执行后仍受阻 → 不再第三次自试，收尾如实结束——复盘
     建议是对已受阻项的修正指引，不是无限重试授权
   - 不再需要更多信息就立即收尾。规划轮数上限 {max_rounds} 轮，超限后系统
     会强制收尾（基于已有工具返回如实作答），不存在无限追问
6. 用户质疑/催促执行（"你真显示了？""到底跳了没？""别光说，带我去啊"）：
   - 真实性询问（质疑某操作是否真执行过/执行细节，如"屏幕上写了什么"）→
     看页面上下文 recent_executions=（跨轮执行记忆：本会话 checker 验收过的
     执行记录，格式"· 动作行"；与 conversation_summary 同性质——系统确认
     事实，不扩展不编造）。记录里有对应执行 → 选 chat 直接收尾，据记录如实
     转述（含「」内实际内容/路径/开关状态），不规划任何工具、不重发；
     记录里没有对应执行 → 也选 chat 收尾，如实说"系统记录里没有这次执行"，
     不编造、不否认回执、不为了"补做"重新规划执行
   - 若质疑的是"某页面/内容是否存在"（"真有这个页面？""确定有这篇？"）→
     content_query 查证后据实作答（页面存在性是内容问题，不是执行真实性）
   - 再次要求（明确重发同款或升级指令——"别光说，带我去啊"= 要直接跳过去、
     "再显示一次刚才那句"）→ 属新请求：重新规划该动作技能并真实执行；
     navigate 填 mode=direct（免确认框直达）；不得零工具口头承诺
     "马上带你去/这就去"——上次正是口头说"已经在 X 页"才被质疑
7. 输出严格按以下格式（JSON 双引号），不要任何其他文字：
SKILL: <技能名>
PARAMS: <JSON>
（多步链中间轮可另加一行：TODO: <步骤1> → <步骤2>，只描述本轮之后的
后续依赖步骤，单步/收尾轮不写）

用户消息：{user_msg}"""


# planner 可规划执行的查询工具清单（与 skills.py _CALLABLE_QUERY_TOOLS 同步；
# 动作工具不在此列——planner 无法经 calls 通道越权动作，只能由技能模板展开）
_QUERY_TOOLS_DESC = """\
- search_notes(keyword)：按关键词搜文章（标题+内容），返回候选列表（含 id/标题/描述/封面）
- rag_search(query)：语义相关度检索，返回行式候选（type/id/score/标题/命中节，用于定位，
  不给全文）
- get_article_detail(article_id, doc_type=note|talk|board|announcement)：读指定文档全文
  （article_id 只能取上一轮工具返回中的真实 id）
- list_notes(page, page_size)：分页列文章
- list_guestbook() / list_talks() / get_announcements() / get_current_time()：
  无参数据直取（留言/说说/公告/当前时间）"""


def _tools_desc() -> str:
    """完整工具清单注入（execute 会执行到的工具都在 _TOOL_MAP 里）。"""
    return "\n".join(
        f"- {t.name}: {t.description}" if t.description else f"- {t.name}"
        for t in _TOOLS
    )


# ---------------------------------------------------------------------------
# 容错解析工具（planner 文本输出 → 结构化）
# ---------------------------------------------------------------------------

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
    lines = [
        f"SKILL={plan_obj['skill']}",
        f"PARAMS={json.dumps(plan_obj.get('params', {}), ensure_ascii=False)}",
        f"TOOLS: {tools}",
        f"NOTE: {plan_obj.get('note') or '（无）'}",
    ]
    # TODO 行是可选第 6 行：插在 REPLY 之前（REPLY 的 DOTALL 解析假设它是末行）
    todo = plan_obj.get("todo") or []
    if todo:
        lines.append(f"TODO: {' → '.join(todo)}")
    lines.append(f"REPLY: {plan_obj['reply']}")
    return "\n".join(lines)


def _parse_todo(raw: str) -> list:
    """提取 TODO 行剩余步骤列表（可选第 6 行契约；planner_node 与 parse_plan 共用）。

    容错：按 [→>] 拆段、去行首序号、剥空白与句末标点，空段/“（无）/无/暂无”
    不计。多步链的中间轮才有内容；单步/收尾轮返回空列表。
    """
    tm = re.search(r"TODO\s*[:=]\s*(.+)", raw or "", re.IGNORECASE)
    if not tm:
        return []
    out = []
    for s in re.split(r"[→>]", tm.group(1)):
        s = re.sub(r"^\s*\d+[.)、]\s*", "", s).strip().strip("；;，,。")
        if s and s not in ("（无）", "无", "暂无"):
            out.append(s)
    return out


def parse_plan(raw: str) -> dict:
    """解析 plan 字段（契约的读端）。容错：解析失败 → 按 chat 兜底（宁可少干活，不硬猜）。

    返回 {"skill", "params", "tools", "note", "reply", "todo", "chat"}。
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
    todo = _parse_todo(raw)
    return {
        "skill": skill if skill in SKILL_MAP else "chat",
        "params": params,
        "tools": tools,
        "note": note,
        "reply": reply,
        "todo": todo,
        "chat": (skill in SKILL_MAP and SKILL_MAP[skill].chat) or skill == "chat",
    }


# ---------------------------------------------------------------------------
# 消息/上下文工具
# ---------------------------------------------------------------------------

def _msg_text(m) -> str:
    """多模态 content 兼容：数组（image_url+text 块）只取 text 文本部分。

    分类/注入只消费文本——base64 dataURL 不进 prompt，否则 content[-500:]
    截到图片垃圾。
    """
    content = getattr(m, "content", "")
    if isinstance(content, list):
        return "".join(c.get("text", "") for c in content
                       if isinstance(c, dict) and c.get("type") == "text")
    return content if isinstance(content, str) else str(content)


# ── 页面操作指南（20260905：留言引导幻觉修复的确定性知识源）──
# 背景：3660 事故（用户问"怎么留言"，narrator 编出"昵称/邮箱输入框+右上角登录"
# 全套幻觉流程）——RAG 语料只有文章，没有留言板操作说明；模型没有真凭实据时
# 用"一般论坛经验"脑补填坑，叙述纪律 2 只禁"站内内容编造"、没盖住操作流程类。
# 解法：current_url 命中留言板时把真实 UI 流程以页面事实注入 page_ctx（planner
# 与 model 均可见），模型只转述；任何页面都适用的第 10 条纪律禁止无据脑补 UI。
# 文案与 RiverBoard/index.tsx 实际 UI 对齐（输入框/留名/匿名/我的河灯页签）。
GUESTBOOK_GUIDE = (
    "【河灯集留言板操作指南】（系统注入的页面事实，教访客如何操作时以此为准）"
    "页面下方有留言输入框（提示语「此刻想说的话…」），在框里写好内容即可放灯；"
    "留名框在输入框旁，默认预填当前登录账号昵称，清空留名或点「匿名」则以无名/"
    "匿名身份放灯；不需要注册或邮箱，输入框一直可见。"
    "放灯后页面顶部「我的河灯」页签可查看自己放过的灯。"
    "注意：本页面没有「昵称+邮箱+提交」式表单，也不需要先登录才能留言。"
)

# 留言板路径（/guestbook 与旧隐藏地址 /he 同页）
_GUESTBOOK_URL_RE = re.compile(r"/(?:guestbook|he)\b")


# ── 站内板块与技能清单（20260905：介绍"博客有哪些板块/你能做什么"讲不全的
# 确定性知识源）──
# 实证（trace 20260905T190827/190857/190926/191007）：narrator 看不到板块全图与
# 技能清单——"小猫咪你都可以做什么呀"只列闲聊/找文章/跳转/特效，刚玩过的
# IoT 设备显示与河灯留言都漏；"只有这些吗"提醒后才补、仍漏 IoT；"博客都有
# 哪些功能"靠检索文章撞出 2 个板块（AI 助手/IoT 控制台），留言板/说说/归档等
# 未命中即缺失；近义重问还模板复读同文（489=489）。纪律 2 要求"页面存在"须
# 有据——板块事实不注入，narrator 只能靠记忆碎片或检索命中拼。解法：常驻注入
# 页面上下文（planner 与 model 均可见，模型只转述），与 GUESTBOOK_GUIDE 同构。
# 板块路径与 NAV_MAP（skills.py 单一事实来源）保持一致，新增板块须同步此处与
# test_skills 断言。
SITE_GUIDE = (
    "【站内板块与技能清单】（系统注入的事实——介绍「博客有哪些板块/功能」或"
    "「你能做什么」时以此为准完整转述）站内板块：首页；文章（/article/<id> 单篇）；"
    "留言板=「河灯集」（/guestbook）；说说（/talk）；归档/时间轴（/times）；"
    "关于我（/about）；物联网平台控制台（/device-console/）；登录（/login）与"
    "后台管理（/dashboard）仅博主使用。"
    "你能做的：陪聊与回答站内问题；搜索/找文章并给链接，讲解访客正在读的文章；"
    "查说说、河灯留言、公告；跳转到上述任意板块；开关特效（樱花/雨/雪等）与"
    "夜间模式；让接入的 ESP32 OLED 屏幕显示文字、查询设备在线状态；看访客发来"
    "的图片并描述内容/颜色。介绍能力时按此完整列出，不要遗漏。"
)


def _attach_page_guide(page_ctx: str) -> str:
    """页面上下文常驻附板块/技能清单；命中留言板时再附操作指南（URL 是系统
    上报事实，非模型推断；两份指南同为"只转述"系统数据）。"""
    try:
        out = (page_ctx or "") + "\n" + SITE_GUIDE
        if _GUESTBOOK_URL_RE.search(page_ctx or ""):
            out += "\n" + GUESTBOOK_GUIDE
        return out
    except Exception:
        return page_ctx or ""


def _page_ctx(messages: list) -> str:
    """提取前端实时上报的页面上下文（page/title/特效/夜间），注入 planner/model。

    前端每轮请求都携带真实 current_url（window.location.href），_build_messages
    写入首条 [System: ...] 消息。planner/model 若只凭对话推断访客位置会脱节：
    用户手动转跳后对话历史不体现页面变化（曾见用户说"已经离开物联网控制台了，
    在首页"，模型仍延续上一轮的设备显示动作）。此处显式提取注入 prompt——
    事实以系统上报为准，不依赖模型推断。
    """
    for m in messages:
        content = _msg_text(m) or ""
        found = re.search(r"\[System:\s*(.*?)\]", content, re.DOTALL)
        if found:
            return _attach_page_guide(found.group(1).strip())
    return "（无）"


def _last_user_msg(messages: list) -> str:
    """当前请求的用户消息 = 最近一条 HumanMessage 的文本。

    注意不能取 messages[-1]：planner ⇄ execute 多轮循环时最后一条是 ToolMessage
    （planner 每轮回来看工具返回），只有首轮的最后一条才是用户消息。
    """
    for m in reversed(messages):
        if isinstance(m, HumanMessage):
            return _msg_text(m)[-500:]  # 只看最近一段，防止超长输入稀释决策
    return ""


def _recent_tail(messages: list, max_turns: int = 4, per: int = 160) -> str:
    """最近几轮对话节选（planner 语境补丁，20260903 nav_param_anchor_about 事故）。

    planner 是单消息决策（history-blind），用户催促/质疑（"你不直接转跳过去？"）
    所指的目标只存在于更早轮次里——不给节选就无法还原该跳哪页。取状态消息里
    最近几轮人机对话行（跳过工具帧——结果有专门区块）。跳过两样：注入的页面
    上文（[System:…] 开头的人类消息，planner 已有 page_ctx）与当前这条用户消息
    （它是决策对象，不是上下文）。逐条截断防超长输入稀释决策。
    """
    out: list[str] = []
    seen_current = False
    for m in reversed(messages):
        if not isinstance(m, (HumanMessage, AIMessage)):
            continue
        text = (_msg_text(m) or "").strip()
        if not text:
            continue
        if isinstance(m, HumanMessage):
            if text.startswith("[System:"):
                continue
            if not seen_current:  # 最近的用户消息 = 当前请求，不算上下文
                seen_current = True
                continue
            speaker = "用户"
        else:
            speaker = "泠月"
        text = text.replace("\n", " ")[-per:]
        out.append(f"{speaker}：{text}")
        if len(out) >= max_turns:
            break
    if not out:
        return "最近对话节选：（无更早轮次）"
    return ("最近对话节选（判断'催促/质疑'所指——目标通常在这些轮次里）：\n"
            + "\n".join(reversed(out)))


def _has_frames(messages: list) -> bool:
    """当前请求是否有工具执行帧（ToolMessage）。"""
    return any(isinstance(m, ToolMessage) for m in messages)


# get_article_detail 全文帧的节选上限：该帧是 narrator 引用文章细节的唯一依据，
# 深文事实常在文末（20260903 golden 实证：架构文档 note 19 的
# STREAM_TOTAL_TIMEOUT 在全文 19260 字符处、固件参考 note 14 的 esp_https_ota
# 在 3754 处，per=300 的旧截断让整条 rag 深文族 FAIL）。20000 覆盖站内全部
# 文章正文长度，超出部分带"节选"标注——narrator 不会把截断当全文。
_DETAIL_FRAME_PER = 20000


def _frame_texts(messages: list, limit: int = 5, per: int = 300) -> str:
    """最近的工具返回摘要（planner 下一轮决策依据 / narrator 叙述依据）。

    只取最近 limit 条。截断策略按帧型：普通帧（检索候选/列表）截 per 字符
    （行式精简，够看）；get_article_detail 是全文读取帧，按 _DETAIL_FRAME_PER
    大幅放宽并标注"节选"；__ERROR__ 信息完整保留（planner 需要据错误修正参数
    重试）。
    """
    frames = [m for m in messages if isinstance(m, ToolMessage)]
    if not frames:
        return "（本轮尚无工具执行）"
    parts = []
    for m in frames[-limit:]:
        name = getattr(m, "name", "") or ""
        text = _msg_text(m)
        if text.startswith("__ERROR__"):
            parts.append(f"工具 {name} 返回错误: {text}")
        elif name == "get_article_detail" and len(text) > _DETAIL_FRAME_PER:
            parts.append(
                f"工具 {name} 返回（节选，原文过长仅示前 {_DETAIL_FRAME_PER} 字）: "
                f"{text[:_DETAIL_FRAME_PER]}")
        elif name == "get_article_detail":
            parts.append(f"工具 {name} 返回: {text}")
        else:
            parts.append(f"工具 {name} 返回: {text[:per]}")
    return "\n".join(parts)


def _receipts_text(receipts: list) -> str:
    """checker 验收回执摘要（narrator 同轮如实转述依据，20260904）。

    工具帧是"执行了什么"的原始返回，回执是"系统验收确认执行成功"的收据——
    narrator 描述"实际显示了什么/跳转到哪"以回执为准（帧可能只含 ack 不含
    参数，回执的 args 是文案注入后值，含实际屏文）。空 → 本轮无已验收执行。
    """
    if not receipts:
        return "（本轮没有已验收的执行）"
    lines = []
    for r in receipts[-5:]:  # 同轮多 spec 时只取最近 5 条，防稀释
        args_txt = json.dumps(r.get("args") or {}, ensure_ascii=False)[:160]
        lines.append(f"- {r['tool']} args={args_txt} → {str(r.get('result', ''))[:160]}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 声称检查正则族（gate 确定性兜底用；作用域见 _claim_issue）
# ---------------------------------------------------------------------------
# 背景（问题记录 20260828-0902）：执行器时代三层声称闸（执行声称/读取声称/
# 工具调用声称）+ LLM 质检 + 预算耗尽 accept 的防幻觉组合，被"措辞绕行"与
# "质检采信模型自称"击穿。20260903 重构后自由 ReAct 已废除：所有执行都经
# execute 确定性发生（有执行必有帧），检查层只剩 gate 兜模型叙述失真——
# 这些正则的作用域大幅收窄（见 _claim_issue 注释），宁可漏拦不可误伤
# （gate 的 fallback 会吞掉整轮叙述，误伤成本高）。
# 执行声称词（20260828 影子系统重构）：回复含这些词即构成"已对设备/页面执行了
# 操作"的声称。程序可查的事实：声称必须有工具返回支撑（轨迹里有 ToolMessage），
# 否则就是编造。注意词表不含"开启/关闭/切换"（effect/darkmode 幂等轮合法陈述
# "樱花已经开着"来自 current_effects，不得误伤）；err 帧场景的开关声称由
# _COMPLETION_CLAIM_RE 兜。
_EXECUTION_CLAIM_RE = re.compile(
    r"已(?:经)?(显示|写入|写下|写好|写上去|上屏|发送|下发|执行|展示|打上|放上|刷新|设置)"
    r"|成功(?:显示|写入|下发|发送|执行)"
)
# err 帧场景的完成式声称（gate 仅在工具帧含 __ERROR__ 时使用）：工具失败了
# 回复还称"已跳转/已开启/已完成"= 把失败说成成功。
_COMPLETION_CLAIM_RE = re.compile(
    _EXECUTION_CLAIM_RE.pattern
    + r"|已(?:经)?(跳转|到达|切换|开启|关闭|打开|完成|成功)"
    + r"|成功(?:跳转|切换|开启|关闭|到达)"
)
# 读取声称族（20260831 补，21:19:40 事故实证：chat 轮声称"回去重读"文章但零工具
# 调用，引用 6 处全文细节 5 处不存在）——声称"读了/查了博客内容"必须以工具返回
# 为据。仅 content_query 异常零工具轮启用（宽查）：该轮"本该有帧"，声称误伤
# 成本低。chat 轮不启用（chat 不涉及"读站内内容"，命中即确凿异常）。
# 模式收敛（宁漏勿误伤：只看"读/查/看"+内容宾语与"重读"类，不抓裸"看了"）。
# 20260901 事故补丁：模型声称"查的是[关于页]""把整个博客扫了一遍""找到几条…文章
# 链接"（零工具调用，7 个 /article/61/59/57/62/64/68/71 全部 404）——"查的是X页"、
# "扫了一遍"、"找到N条"类表述同样构成读取声称，纳入模式（宾语限页面/博客/内容域，
# 不抓"找到工作/找到钥匙"类生活语）。
# 20260902 事故补丁（025744 实证："您让我查的这两条，我读完了"零工具编造，文章
# 17/35 不存在）：三种表述漏网——①"查的这两条"（缺"是/就是"、以量词"条"结尾）；
# ②裸"读完了"（宾语缺失）；③"查了两篇文章"（量词"两篇"插入动词与宾语之间）。
_READ_CLAIM_RE = re.compile(
    r"重读|重看|重新读|重新看|回去读"
    r"|已(?:经)?读取|已?通读"
    r"|(?:都|全部|基本)?读完了?(?:全文|文章|内容|文档|这篇|那篇|博客|[。，；!？!?～~\n🐾喵]|$)"
    r"|(?:我|咱|喵)?(?:刚|刚才|刚刚|已经?)?(?:读|看|查|翻|搜|检索)(?:过|了|完|遍)(?:了)?(?:这|那)?(?:一|两|三|几|数|[一二三四五六七八九十0-9]*)?(?:条|篇|个|本|些)?(?:相关|有关|的)?(?:全文|文章|内容|文档|留言|说说|博客|链接|帖子)"
    r"|(?:这|那)?[一二三四五六七八九十0-9]*(?:条|篇|个|本)(?:留言|说说|文章|消息|内容|链接)(?:我|咱|喵)?(?:都|全部)?(?:读|看|查|翻)(?:过|了|完|遍)(?:了)?"
    r"|(?:核对|核实|查验)(?:过)?(?:全文|文章|内容|文档)"
    r"|查的(?:是|就是)?(?:(?:这|那)?[一二三四五六七八九十0-9]*(?:条|篇|个)(?:留言|说说|文章|消息|内容|链接)?|[^，。！？!?～~\n]*?(?:页|页面|博客|文章|内容|正文))"
    r"|(?:我|咱|喵|泠月喵)?(?:刚|刚才|刚刚|已经?|真的)?(?:把|去|到)?(?:整个)?(?:博客|网站|站点|文章库|站内|系统)(?:里|上面)?(?:都|全部|整个)?(?:扫|查|翻|搜|翻找|查找|检索)(?:了)?(?:个)?(?:一遍|一圈|遍|好几圈)"
    r"|(?:两|双)(?:边|侧|个)(?:板块|数据源)?(?:都|也)?(?:真的)?(?:翻|查|看|搜)(?:了|过|完)(?:了)?"
    r"|找(?:到|出|出了)(?:了)?(?:几|数)?[一二三四五六七八九十0-9]*(?:条|篇|个|些)(?:[^，。！？!?～~\n]{0,20}?)?(?:文章|链接|博客|内容|文档|东西)"
)
# 工具调用声称族原始版（content_query 异常零工具轮宽查用；20260902 133535 实证
# 原词："刚才那两条我都调用了工具……get_current_time"）：零工具轮点名具体工具名
# = 声称调用过。content_query 宽查场景下宁可信其为声称。
_CALLED_TOOL_CLAIM_RE = re.compile(
    r"调(?:用|过)(?:过)?(?:了)?(?:工具|get_current_time|rag_search|list_guestbook|list_talks|get_announcements|get_article_detail|search_notes)"
    r"|调用了?(?:这个|那个|这些|两个|几个|三个)?工具"
    r"|(?:get_current_time|rag_search|list_guestbook|list_talks|get_announcements|get_article_detail|search_notes)"
)
# chat 零工具轮的窄声称（20260902 133535 事故后设计）：必须"第一人称 + 工具
# 相关动词"才算自称调用了工具——第三人称/概念性提及（"防止模型假装调用了
# 工具"这类知识讨论、引用访客的话"你说我调用了工具"）不命中，避免误伤。
# 宁可漏拦（还有叙述纪律 + trace 抽检），不可误伤（fallback 吞整轮）。
# chat 零工具轮的站内扫描声称（20260905 18:19 实证：chat 轮编"去站内翻找了一
# 圈"——无工具名、无"调用/用"动词、主语是名字自称"泠月喵"，_CHAT_TOOL_CLAIM_RE
# 与 _READ_CLAIM_RE（chat 轮不启用）均不命中）。窄模式：必须含站内空间词 + 完成式
# 扫描动量词，精确拦"系统性检索"形态；口语"看了看/找找/翻翻"（未完成）不命中。
_CHAT_SCAN_CLAIM_RE = re.compile(
    r"(?:我|咱|人家|本喵|泠月喵|喵)?(?:刚|刚才|刚刚|这轮|已经?|确实|真的|又)?"
    r"(?:把|去|到|在)?(?:整个)?(?:站内|博客|网站|站点|文章库|系统)(?:里|上|上面)?"
    r"(?:都|全部|整个)?(?:扫|查|翻|搜|翻找|查找|检索)(?:了)?(?:个)?(?:一圈|一遍|个遍|好几圈|个底朝天)"
)
_CHAT_TOOL_CLAIM_RE = re.compile(
    r"(?:我|咱|人家|本喵)(?:刚|刚才|刚刚|这轮|这一轮|之前|确实|真的|又|就|已经?|都|把)?(?:用|通过|拿|调)(?:了|过)?(?:get_current_time|rag_search|list_guestbook|list_talks|get_announcements|get_article_detail|search_notes|工具)"
    r"|(?:我|咱|人家|本喵)(?:刚|刚才|刚刚|这轮|这一轮|之前|确实|真的|又)?调(?:用|过)(?:过)?(?:了)?工具"
    r"|(?:我|咱|人家|本喵)刚(?:刚|才)?(?:用|通过)(?:get_current_time|rag_search|list_guestbook|list_talks|get_announcements|get_article_detail|search_notes)(?:查|搜|调|读|看|翻|拿|执行)"
)
# 命令前缀文本：回复正文出现系统命令帧前缀 = 模型在"假装发命令"（旧事故：正文
# 输出 AUTO_NAVIGATE:/NAVIGATE:/EFFECT:/DARKMODE: 文本既不会执行、还误导用户
# 以为已执行）。任何轮次命中一律兜底——叙述纪律已禁止，命中即确凿违规。
_CMD_PREFIX_RE = re.compile(r"(?:AUTO_NAVIGATE|NAVIGATE|EFFECT|DARKMODE)\s*[:：]")
# 确认式导航 + 完成式到达声称（NAVIGATE: 帧 = 等待确认，非已跳转；曾见模型返回
# NAVIGATE: 后回复"已经带您到文章页"，用户视角即幻觉）。仅 navigate 技能轮启用。
_NAV_ARRIVAL_RE = re.compile(
    r"(已经?带|已经?到|已经?跳转|跳转成功|成功[^\n。，,]*?(跳|转)|过去了|已经?去)")
# NOTE 零工具（页面不存在/已下线）轮的如实措辞核验词表（与 instantiate_plan 的
# note 文本配套，见 gate_node）。
_HONEST_DOWN = ("下线", "下架", "无法访问", "没有了")
_HONEST_GONE = ("没有", "不存在", "找不到", "无法识别", "没有找到")


def _claim_issue(reply: str, skill: str, plan: dict, frames_exist: bool) -> tuple[str, str] | None:
    """声称闸判定（gate 确定性兜底，20260902 事故族）：回复含声称但轨迹无工具
    支撑 → 返回 (issue, 人设内 fallback 文本)；有据/无声称 → None。

    作用域（20260903 收窄后的设计）：
      - 任何轮：命令前缀文本（_CMD_PREFIX_RE）
      - chat 零工具轮：仅第一人称工具调用声称（_CHAT_TOOL_CLAIM_RE）——高精确
        模式；"重读/查过"读取声称不在此拦（chat 轮多为口语，误伤成本高，
        且 chat 计划 TOOLS 恒空、站内内容声称本就不该出现——留给叙述纪律）
      - content_query 零工具轮（异常路径：计划本应有调用清单却留空收尾）：
        三族全查（读取/执行/调用声称）——该场景"本该查证"，声称误伤成本低
      - 有帧轮：读取/调用声称天然有据，不做文本对照；只兜 err 帧 + 完成式
        声称、NAVIGATE: 确认帧 + 到达声称（见 gate_node）
    """
    if _CMD_PREFIX_RE.search(reply):
        return ("cmd_prefix", _FALLBACK_CMD_PREFIX)
    if frames_exist:
        return None  # 帧存在：声称有据（err 帧/确认帧场景由 gate_node 单独兜）
    if skill == "chat":
        if (_CHAT_TOOL_CLAIM_RE.search(reply) or _CHAT_SCAN_CLAIM_RE.search(reply)):
            return ("claim_without_tool", _FALLBACK_CLAIM)
        return None
    if skill == "content_query":
        if (_READ_CLAIM_RE.search(reply) or _EXECUTION_CLAIM_RE.search(reply)
                or _CALLED_TOOL_CLAIM_RE.search(reply)):
            return ("claim_without_tool", _FALLBACK_CLAIM)
    return None


# gate fallback 文本（人设内、直接给访客看——validate→fallback，无 REVISE 重考）
_FALLBACK_CMD_PREFIX = (
    "喵呜……主人，我刚才的回复里混进了不该出现的系统命令文本，已经被我拦下啦"
    "（正文里的命令不会生效的）。你真正想要的跳转/特效/夜间模式，直接告诉我要"
    "做什么，我让系统执行给你看～")
_FALLBACK_CLAIM = (
    "喵呜……被主人抓包啦。这一轮系统记录里其实没有任何工具执行，我刚才说自己"
    "查过/读过/调用过是不对的——没核实过的事不能装成核实过的样子。你愿意的话"
    "再问我一次，我让系统认认真真查一遍再回答你，好嘛？")
_FALLBACK_EMPTY = (
    "喵呜……主人，我刚才好像卡住了，没能说出话来。可以再问我一次嘛？这次我让"
    "系统查清楚了再好好回答～")
_FALLBACK_ERR_CLAIM = (
    "呜……主人对不起，刚才那条操作系统返回的是失败（执行出错了），我却不小心"
    "说成了已完成——不骗你，实际没有成功。要不要我再试一次？")
_FALLBACK_NAV_PENDING = (
    "等一下喵～刚才那条跳转还在等主人确认，页面其实还没有过去，我不该说'已经"
    "带你到了'。你在弹窗里点一下确认，或者直接说一句'直接跳转'，我马上让系统"
    "带你过去～")
_FALLBACK_URL = (
    "喵呜……主人，我刚才给的资源链接其实没有系统依据——站内真实资源我没查到，"
    "不能拿编造的地址给你。先别急着点，等我让系统查到真实地址再给你，好不好？")
_FALLBACK_DOWN = (
    "喵呜……那个板块确实已经下线了，刚才说得好像还能去一样，是我不好。现在站里"
    "能逛的真实页面是：首页、留言板、说说、时间轴、关于我～要去哪边嘛？")
_FALLBACK_GONE = (
    "喵呜……主人，那个页面我在站里确认过是不存在的，刚才不该说得像真的一样。"
    "站里真实能去的页面有：首页、留言板、说说、时间轴、关于我、登录、管理后台、"
    "物联网平台。要不要我带你逛逛？")


def _fallback_result(issue: str, text: str, plan: dict, frames: int) -> dict:
    """gate fallback 收尾（validate→fallback：检查不通过即收尾，无重考轮）。

    返回带 done=True + [Fallback 决定] SystemMessage + fallback_text 的 state
    更新——server.py 据此执行 __RESET__ + 以 fallback 文本作为最终回复
    （fallback 是给访客的如实回复，不是"修正要求"——与旧 REVISE 语义不同）。
    """
    record("gate", "fallback", issue=issue, skill=plan["skill"], frames=frames)
    logger.info("[gate] fallback（%s）: skill=%s frames=%d", issue, plan["skill"], frames)
    return {"done": True,
            "messages": [SystemMessage(content=f"[Fallback 决定]: {text}")],
            "fallback_text": text}


# ---------------------------------------------------------------------------
# 3. Node：planner（唯一决策）/ execute（确定性执行）/ model（narrator）/ gate
# ---------------------------------------------------------------------------

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

_NAV_VERB_RE = re.compile(
    r"^(?:小猫咪|喵喵|主人|猫猫|喵)?[,，、\s]*"
    r"(?:去一下|回到|返回|跳转到|前往|转到|转跳|打开|进入|带我(?:去|到)|去|进|回|到|访问)"
    r"\s*([^\s，。！？!?～~、；;：:]{1,8})$"
)


# 当前文章读取确定性快道（20260901 系统性修复，零 LLM）。
# 根因（用户评审定性，声称闸补丁被拒）：模型对"用户当前在读的文章"只有
# page_ctx 文本提示（current_url=/article/21），无结构化事实、无强制读取——
# 于是模型凭 URL 文本知道在读哪篇、却永远不真的读，回答全靠想象。事故实证：
# 232107「这篇文章你怎么看」→ 模型声称"这篇我读完了"编造 600 字全文细节。
# 20260903 架构后依然保留：这是固定流程任务——current_url 解析出文章 ID 是
# 系统数据（非模型推断），计划 TOOLS 行强制 get_article_detail → execute 必须
# 调用（execute 无自由意志，比旧 reflector 兜底更硬）。
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


# 特效切换快道（20260904）：把 X 换成/改成 Y → 关当前效果 + 开目标效果双 spec。
# 事故实证：planner LLM 对"不要樱花了，改成下雨吧"反复只解出"关樱花"半边——
# 10 轮采样 8 轮丢 rain:on（4 轮收尾"只关了樱花"、4 轮明言"雨没法帮你切换"），
# 目标效果半边的规划在模型侧不稳定。切换是固定流程任务：旧效果来自 current_effects
# 系统状态、目标效果是消息动词后的字面量，无模型推断空间 → 与 read_article 快道
# 同理（宁多勿漏：误触发成本 = 一次幂等检查，漏触发 = 用户要求只做一半）。
_EFFECT_ALIASES = {  # 别名 → 规范化 effect id（匹配按别名长度降序，长名优先）
    "樱花": "sakura", "sakura": "sakura",
    "下雨": "rain", "大雨": "rain", "rain": "rain", "雨": "rain",
    "雪花": "snow", "下雪": "snow", "snow": "snow", "雪": "snow",
}
_SWITCH_VERB_RE = re.compile(r"换成|改成|改为|切换|调成|变为|换")
# 内容改写语境排除（"把文章里的雨字改成雪字"不是特效请求）——不用"文章/留言"
# 这类词（会误杀"换特效顺便查文章"的混合意图），只排改写对象的强标记词
_EFFECT_TALK_GUARD = re.compile(r"内容|文字|标题|代码|字|词|称呼|名字")


def _effect_switch_fast_path(user_msg: str, current_effects: str) -> dict | None:
    """特效切换快道：切换动词 + 目标效果名（消息动词后）→ effect 双 spec 计划。

    返回 plan dict（与 instantiate_plan 产物同构，tools 可含两条 toggle_effect），
    或 None 落回 planner LLM。旧效果取值顺序：消息点名（动词前）→ 当前开着且
    非目标的其它效果（"改成下雨"不点名时以 current_effects 实况补旧）；
    目标已开着时只关旧（幂等，不重复开）。
    """
    if _EFFECT_TALK_GUARD.search(user_msg):
        return None
    m = _SWITCH_VERB_RE.search(user_msg)
    if not m:
        return None
    after = user_msg[m.end():]
    before = user_msg[:m.start()]
    # 目标效果 = 动词后第一个别名命中（长名优先：先试"下雨"再试"雨"）
    target = None
    for alias in sorted(_EFFECT_ALIASES, key=len, reverse=True):
        if alias in after:
            target = _EFFECT_ALIASES[alias]
            break
    if target is None:
        return None
    old = None
    for alias in sorted(_EFFECT_ALIASES, key=len, reverse=True):
        if alias in before:
            old = _EFFECT_ALIASES[alias]
            break
    cur = {e for e in (current_effects or "").split(",") if e and e != "none"}
    if old is None:
        on_others = [e for e in cur if e != target]
        old = on_others[0] if on_others else None
    if old == target:
        return None  # 换到当前已开效果 = 幂等，落回 planner 叙述
    tools = []
    if old is not None and old in cur:
        tools.append(f"toggle_effect({json.dumps({'effect': old, 'action': 'off'}, ensure_ascii=False)})")
    if target not in cur:
        tools.append(f"toggle_effect({json.dumps({'effect': target, 'action': 'on'}, ensure_ascii=False)})")
    if not tools:
        return None
    note = f"特效切换快道（确定性，非模型决策）：{'、'.join(tools)}"
    return {
        "skill": "effect",
        "tools": tools,
        "note": note,
        "reply": SKILL_MAP["effect"].reply_contract,
        "chat": False,
        "params": {"effect_switch": f"{old or '（无）'}→{target}"},
    }


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
# "显示内容由 execute 在工具调用时创作"（execute 内 _create_display_text）。
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
    内容由 execute 节点创作（PARAMS 不填 text，见 _create_display_text）——屏幕
    文案不进 planner 文本通道，杜绝"指令原文残缺片段上屏"。
    """
    if _QUESTION_RE.search(user_msg) or _NEGATION_RE.search(user_msg):
        return None
    if not _DISPLAY_FAST_RE.search(user_msg):
        return None
    plan_obj = instantiate_plan("device_display", {})
    plan_obj["params"] = {}
    logger.info("[planner] 显示意图快道命中（零 LLM，内容由 execute 创作）")
    return plan_obj


# 检索候选行解析（确定性拦截用，见 planner_node"检索重复清单拦截"）
# 经验记录类标题：机制型问题的答案在「参考/指南」类文档，这类标题延后读。
_EXPERIENCE_TITLE_RE = re.compile(
    r"问题与解决记录|问题记录|踩坑|复盘|FAQ|排错|故障|心得|备忘")
# rag_search 行式候选（例：`1. type=note id=19 score=5.82 title=… 命中节=…`）
_RAG_ROW_RE = re.compile(
    r"^\s*\d+\.\s*type=(\w+)\s+id=(\d+)\s+score=[\d.]+\s+title=(.*)$", re.M)
_DETAIL_SPEC_RE = re.compile(r'article_id["\']?\s*[:=]\s*(\d+)')


def _candidate_detail_plan(messages: list, executed: list) -> dict | None:
    """重复拦截的确定性出路：从最近检索帧候选行里挑第一个未读文档读全文。

    候选顺序 = 帧内行序（检索相关性序）；经验记录类标题在存在机制文档时延后
    （20260903 rag_ota_http 实证：关键词只命中《问题与解决记录》踩坑史）。
    候选全已读 / 无候选 → None（调用方直接收尾，不浪费轮次）。
    """
    done_ids = {m.group(1) for s in executed for m in [_DETAIL_SPEC_RE.search(s)]
                if m is not None}
    frames = [m for m in messages if isinstance(m, ToolMessage)]
    rows: list[tuple[str, str, str]] = []  # (id, doc_type, title)
    for m in reversed(frames):
        name = getattr(m, "name", "") or ""
        text = _msg_text(m)
        try:
            if name == "search_notes":
                obj = ast.literal_eval(text)
                if isinstance(obj, list):
                    for r in obj:
                        if isinstance(r, dict) and r.get("noteKey") is not None:
                            rows.append((str(r["noteKey"]), "note",
                                         str(r.get("noteTitle") or "")))
            elif name == "rag_search":
                for typ, rid, title in _RAG_ROW_RE.findall(text):
                    dt = ("talk" if typ == "talk" else "board" if typ == "board"
                          else "note")
                    rows.append((rid, dt, title.split(" 命中节=")[0]))
        except Exception:
            continue
    # search_notes 候选优先于 rag_search（关键词命中行带标题，语义命中可能是
    # 噪声——20260903 cq_query_embedded_articles 实证：问候词进 rag 后 Git
    # 排第一）。同帧行序即帧内序（相关性序），旧帧行只在没有新候选时兜底。
    seen: set[str] = set()
    ordered: list[tuple[str, str, str]] = []
    for r in rows:
        if r[0] in seen:
            continue
        seen.add(r[0])
        ordered.append(r)
    unread = [r for r in ordered if r[0] not in done_ids]
    if not unread:
        return None
    mech = [r for r in unread if not _EXPERIENCE_TITLE_RE.search(r[2])]
    pick = (mech or unread)[0]
    plan_obj = instantiate_plan("content_query", {"calls": [
        {"tool": "get_article_detail",
         "args": {"article_id": int(pick[0]), "doc_type": pick[1]}}]})
    plan_obj["params"] = {"calls": [{"tool": "get_article_detail",
                                     "args": {"article_id": int(pick[0]),
                                              "doc_type": pick[1]}}]}
    plan_obj["note"] = ((plan_obj.get("note") or "")
                        + f"（确定性改读候选《{pick[2][:24]}》全文）")
    return plan_obj


def _any_error_frame(messages: list) -> bool:
    """帧里是否有 __ERROR__（错误修正重试合法，跳过重复拦截）。"""
    return any(str(getattr(m, "content", "")).lstrip().startswith("__ERROR__")
               for m in messages if isinstance(m, ToolMessage))


def _terminal_plan(has_frames: bool, reason: str) -> dict:
    """确定性收尾计划（不经 LLM）：有工具帧 → content_query 如实收尾；无帧
    → chat 如实说明无法确认。reason 注入 note 说明收尾原因（轮次上限/受阻
    复盘终局共用——reflector wrap_up 与规划超限同性质，不静默 accept）。"""
    if has_frames:
        return {
            "skill": "content_query",
            "tools": [],
            "note": (f"{reason}：基于以上已有工具返回如实收尾作答；工具返回不足"
                     "以回答时如实告知'站内没有找到/暂时无法确认'，不得再用模型"
                     "记忆硬答"),
            "reply": SKILL_MAP["content_query"].reply_contract,
            "chat": False,
        }
    return {
        "skill": "chat",
        "tools": [],
        "note": (f"{reason}且无任何工具执行记录：如实告知暂时无法确认/无法回答，"
                 "不得编造"),
        "reply": "直接回答",
        "chat": True,
    }


def _wrap_up_plan(has_frames: bool) -> dict:
    """规划轮次上限强制收尾计划（确定性，不经 LLM，20260903 语义不变）。"""
    return _terminal_plan(has_frames, f"已达规划轮次上限（{MAX_PLAN_ROUNDS}）")


def planner_node(state: AgentState, config: RunnableConfig | None = None) -> dict:
    """职责（唯一决策点）：选技能 + 填参数 + 给调用清单 → 实例化为计划 → state.plan。

    20260903 架构裁决后的 planner 是"全权"的：知识型问题的检索定位（选
    search_notes 还是 rag_search、抽什么关键词）、是否读全文、何时收尾，全部
    在这里每轮决策；execute 只是执行器。多轮循环：
      planner（首轮决策：给调用清单）→ execute（确定性执行）→ planner（看工具
      返回再决策：读全文/换词再搜/收尾）→ … → 收尾轮（调用清单空）→ model
    结构保证：
      - 每轮执行什么由 planner 文本输出决定，白名单/模板双校验（skills.py）；
      - 动作技能一次决策后（工具帧已可见）planner 必须收尾——绝不重复执行；
      - 循环上限 MAX_PLAN_ROUNDS，超限强制收尾（_wrap_up_plan）。
    与旧版的本质区别（面试点）：执行层的自由度（参数自拟/是否调用/输出权）
    全部收走，规划空间仍受限（技能注册表 + 工具白名单 + 映射表都是系统数据）。
    """
    if _stopped(config):
        logger.info("[planner] cancelled (client disconnected)")
        raise AgentCancelled()

    user_msg = _last_user_msg(state["messages"])
    page_ctx = _page_ctx(state["messages"])
    rounds = state.get("plan_rounds", 0)
    has_frames = _has_frames(state["messages"])

    # 轮次上限 → 强制收尾（不再规划新调用；帧内容足够就让 narrator 如实作答）
    if rounds >= MAX_PLAN_ROUNDS:
        plan_obj = _wrap_up_plan(has_frames)
        logger.info("[planner] 规划轮次上限(%d)，强制收尾", MAX_PLAN_ROUNDS)
        return {"plan": plan_encode(plan_obj), "plan_rounds": rounds + 1, "done": False}

    # 确定性快道只在首轮（rounds==0 且本轮尚无任何工具帧）判定——execute 完成
    # 后控制权回到 planner 时若再命中快道，会重复规划同一动作 → 死循环
    # （设计陷阱 20260903：快道对象是"用户首条消息"，不是"每轮重新评估"）。
    if rounds == 0 and not has_frames:
        # 导航确定性快道（零 LLM）：命中即返回，不调用 planner LLM（耗时大头）。
        nav = _nav_fast_path(user_msg)
        if nav is not None:
            logger.info("[planner] 导航快道命中（零 LLM）: %s", nav["tools"])
            record("planner", "fastpath", kind="nav", tools=nav["tools"], round=rounds)
            return {"plan": plan_encode(nav), "plan_rounds": rounds + 1, "done": False}

        # 显示意图确定性快道（零 LLM）：屏幕类名词+写/显示动词强模式 →
        # device_display 计划（内容由 execute 创作，PARAMS 不填 text）。
        display = _display_fast_path(user_msg)
        if display is not None:
            record("planner", "fastpath", kind="display", round=rounds)
            return {"plan": plan_encode(display), "plan_rounds": rounds + 1, "done": False}

        # 当前文章读取确定性快道（零 LLM，20260901 系统性修复）：用户当前页面是
        # 文章详情页且消息引用"这篇/我正在读"等 → read_article 计划，TOOLS 行
        # 强制 get_article_detail(id)。ID 是系统从 current_url 解析的数据，执行被
        # 计划模板强制、被 execute 确定性执行——零工具声称"读过了"结构上不可能。
        article = _article_fast_path(user_msg, page_ctx)
        if article is not None:
            record("planner", "fastpath", kind="article_read", tools=article["tools"], round=rounds)
            return {"plan": plan_encode(article), "plan_rounds": rounds + 1, "done": False}

        # 特效切换确定性快道（零 LLM，20260904）：把 X 换成/改成 Y → 关旧开新
        # 双 spec 同轮（planner LLM 反复丢目标效果半边，见 _effect_switch_fast_path）。
        eff_cur = re.search(r"current_effects=([^;\]]+)", page_ctx)
        switch = _effect_switch_fast_path(user_msg, eff_cur.group(1) if eff_cur else "")
        if switch is not None:
            record("planner", "fastpath", kind="effect_switch", tools=switch["tools"], round=rounds)
            return {"plan": plan_encode(switch), "plan_rounds": rounds + 1, "done": False}

    # LLM 决策轮。低温度（分类不需要创造力）、小 max_tokens、短超时。
    # enable_thinking=False：planner 是"选技能+填参数"的结构化分类任务（300 token
    # 输出），thinking 思考链纯浪费（实测 13.4s → 预计 2-4s，且波动正来自 thinking
    # 链长度）；与 execute 文案创作/摘要等低 token 调用同一做法。
    llm = get_llm(temperature=0.2, max_tokens=400, timeout=30, enable_thinking=False)
    round_info = (
        f"当前决策：第 {rounds + 1}/{MAX_PLAN_ROUNDS} 轮。"
        + ("本轮已有工具执行帧（见下方结果），决策据此收敛。" if has_frames
           else "本轮尚无工具执行，是首轮决策。"))
    _t0 = time.monotonic()
    logger.info("[planner] LLM 调用开始（round %d/%d）", rounds + 1, MAX_PLAN_ROUNDS)
    try:
        resp = llm.invoke(_PLANNER_PROMPT.format(
            skills_context=build_planner_context(), tools_desc=_QUERY_TOOLS_DESC,
            page_ctx=page_ctx, round_info=round_info,
            recent_context=_recent_tail(state["messages"]),
            tool_results=_frame_texts(state["messages"]),
            reflector_feedback=state.get("issues") or "（本决策轮无复盘建议）",
            max_rounds=MAX_PLAN_ROUNDS, user_msg=user_msg))
    except Exception as e:
        # planner LLM 异常（API 抖动/超时）→ 不炸对话：按收尾兜底如实告知，
        # 有帧就基于帧收尾（narrator 仍能正常叙述），无帧走 chat 诚实答复。
        logger.warning("[planner] LLM 异常，兜底收尾计划: %s", e)
        plan_obj = _wrap_up_plan(has_frames)
        return {"plan": plan_encode(plan_obj), "plan_rounds": rounds + 1, "done": False}
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

    # 字面路径防推断兜底（确定性修正，保留自旧架构）：用户消息里出现 / 开头的
    # 路径且 planner 选了 navigate 时，target 必须原样用该路径——qwen 曾把
    # "/iot" 推断成"物联网平台"（语义替身）→ 计划变成跳转 /device-console/
    # （golden nav_nonexistent 实证）。白名单外的路径经 instantiate_plan 预校验 →
    # 零工具 + "不存在"注记 → 如实告知（与"路径是否有效由系统校验"的设计一致）。
    lit = re.search(r"/[A-Za-z0-9_\-./]+", user_msg)
    if (plan_obj["skill"] == "navigate" and lit
            and plan_obj["params"].get("target") != lit.group(0)):
        logger.info("[planner] 字面路径修正：用户消息含 %s，planner 目标 %r → 强制 %s",
                    lit.group(0), plan_obj["params"].get("target"), lit.group(0))
        plan_obj = instantiate_plan("navigate", {"target": lit.group(0), "mode": "direct"})
        plan_obj["params"] = {"target": lit.group(0), "mode": "direct"}

    # TODO 剩余步骤声明提取（20260904 最小契约）：planner LLM 可选输出行，多步
    # 依赖链的中间轮用它声明"本轮之后还要做什么"——给后续轮次/reflector 看，
    # 不是执行指令（execute 只执行 TOOLS 行）。仅 LLM 决策轮有 raw；快道/拦截
    # 路径的计划是确定性 dict，不带 todo → plan_encode 不写 TODO 行。
    todo = _parse_todo(raw)
    if todo:
        plan_obj["todo"] = todo
        record("planner", "todo", todo=todo, round=rounds)

    # 动作重复执行防护（确定性）：非首轮（已有帧）planner 若仍规划了动作技能
    # （navigate/effect/darkmode/device_display/device_query/read_article）且其
    # 全部工具名都已在帧中出现 → 上一轮已执行，本轮强制收尾不重复执行
    # （动作一次决策即完成，多轮只应发生在 content_query 检索链路——知识型
    # 轮次允许同名检索工具重复（换关键词再搜是合法多轮）。
    if has_frames and plan_obj["tools"] and plan_obj["skill"] in (
            "navigate", "effect", "darkmode", "device_display", "device_query",
            "read_article"):
        frame_names = {getattr(m, "name", "") or "" for m in state["messages"]
                       if isinstance(m, ToolMessage)}
        planned_names = {_tool_name(s) for s in plan_obj["tools"]}
        if planned_names and planned_names <= frame_names:
            logger.info("[planner] 动作已执行（%s），去重收尾", "、".join(sorted(planned_names)))
            plan_obj = _wrap_up_plan(True)
            return {"plan": plan_encode(plan_obj), "plan_rounds": rounds + 1, "done": False}

    # 检索重复清单拦截（20260903 golden 实证：rag_arch_ports planner 把同一
    # rag_search 原句连发 3 轮直到轮次上限——候选 id=19 已命中却从不读全文。
    # content_query 允许"换词再搜"，但原句重发无新信息；候选命中不读全文 =
    # 假收敛）。确定性改判：计划含已执行过的同款 spec → 改读候选行里第一个
    # 未读文档全文（经验记录类标题延后，机制文档优先，见 _candidate_detail_plan）；
    # 无未读候选 → 直接收尾，不浪费剩余轮次。__ERROR__ 帧存在时跳过
    # （错误修正重试合法）。
    # 20260905 变体打转拦截：spec 级判据防"原句连发"，防不住换词变体（231301/
    # 231934 实证 planner 连 4 轮 rag_search 变体，BM25 变体 query 秒回同批文档）。
    # 放宽到工具级计数——判定抽成纯函数 _search_retry_kind（retry_loop/rag_loop），
    # 命中同样确定性转读未读候选全文（planner 已实证靠搜索收敛不了，读全文才有
    # 据收尾）或直接收尾。__ERROR__ 帧存在时整块跳过（错误修正重试合法）。
    if (plan_obj["skill"] == "content_query" and plan_obj["tools"]
            and not _any_error_frame(state["messages"])):
        executed = state.get("executed") or []
        kind = _search_retry_kind(plan_obj, executed)
        if kind:
            dups = [s for s in plan_obj["tools"] if s in executed]
            cand = _candidate_detail_plan(state["messages"], executed)
            if cand is None:
                logger.info("[planner] 检索重复拦截（%s），无未读候选 → 直接收尾",
                            "、".join(dups) if dups else "rag_search 变体 ≥2 次")
                plan_obj = _wrap_up_plan(True)
            else:
                logger.info("[planner] 检索重复拦截（%s）→ 改读候选 %s",
                            "、".join(dups) if dups else "rag_search 变体 ≥2 次",
                            cand["tools"])
                plan_obj = cand
            record("planner", "intercept", reason=kind,
                   dups=dups, redirected=cand is not None)
            return {"plan": plan_encode(plan_obj), "plan_rounds": rounds + 1,
                    "done": False}

    logger.info("[planner] skill=%s params=%s tools=%s（round %d/%d）",
                plan_obj["skill"], plan_obj["params"], plan_obj["tools"], rounds + 1,
                MAX_PLAN_ROUNDS)
    record("planner", "decision", skill=plan_obj["skill"], params=plan_obj["params"],
           tools=plan_obj["tools"], round=rounds)

    return {"plan": plan_encode(plan_obj), "plan_rounds": rounds + 1, "done": False}


# ---------------------------------------------------------------------------
# execute 节点：确定性执行 planner 调用清单（零自由，取代旧 tools_node）
# ---------------------------------------------------------------------------
# 20260903 架构核心：TOOLS 行是"执行清单"而非"允许名单"。execute 不判断
# "要不要调"（planner 已决定）、不产生参数（参数在 TOOLS 行 spec 里，planner/
# 模板侧已定）、没有授权检查分支（清单本身经过 instantiate_plan 白名单校验，
# 动作工具只能由技能模板展开，skills.py 已论证）——它只是忠实执行器。
# 唯一保留的"创作"自由：device_oled_display 的 text=None 时由小型 LLM 结合
# 对话创作屏幕文案（_create_display_text）——这是技能模板的固有设计（屏幕
# 文案由系统在展示时创作，不进 planner 文本通道），非执行层的越权自由。

def _tool_name(tool_spec: str) -> str:
    """TOOLS 行条目 → 工具名（'get_article_detail({"article_id": 21})' → get_article_detail）。"""
    return tool_spec.split("(", 1)[0].strip()


def _search_retry_kind(plan_obj: dict, executed: list) -> str | None:
    """检索重复清单拦截判定（纯函数，20260905 工具级计数扩展）。

    content_query 轮 planner 计划中仍含 executed 里的同款 spec → "retry_loop"
    （原句连发，20260903 判据）；无同款 spec 但本轮仍规划 rag_search 且已执行
    rag_search ≥2 → "rag_loop"（换词变体打转——rule5"换词语义重试"已给足 2 次
    自由检索：首搜 + 一次换词，第三次变体在 BM25 下大概率仍回同批文档，判定
    打转）。都不中 → None（放行）。

    只统计 rag_search：search_notes/list_notes 是确定性点名列（成对点名/多关键
    词链合法），无打转实证；__ERROR__ 帧的修正重试由调用方整块跳过（本函数不
    看 messages）。
    """
    if plan_obj.get("skill") != "content_query" or not plan_obj.get("tools"):
        return None
    if any(s in executed for s in plan_obj["tools"]):
        return "retry_loop"
    if (any(_tool_name(s) == "rag_search" for s in plan_obj["tools"])
            and sum(1 for s in executed if _tool_name(s) == "rag_search") >= 2):
        return "rag_loop"
    return None


def _tool_args(tool_spec: str) -> tuple[dict, bool]:
    """TOOLS 行条目 → (参数字典, 解析是否成功)。spec 参数由 instantiate_plan 以
    json.dumps 落盘（JSON 的 true/false/null 不是 Python 字面量，ast.literal_eval
    会拒），故先 json.loads（规范格式）再 ast.literal_eval（容手写 Python 风格），
    都失败兜底空参 + ok=False——checker 据此判 args_parse（调用方按错误帧处理；
    工具签名必填参数缺失时工具层自会报 __ERROR__，不炸图）。
    """
    m = re.match(r"^(\w+)\((.*)\)$", tool_spec.strip(), re.DOTALL)
    if not m or not m.group(2).strip():
        return {}, True
    raw = m.group(2).strip()
    for loader in (json.loads, ast.literal_eval):
        try:
            obj = loader(raw)
            if isinstance(obj, dict):
                return obj, True
        except Exception:
            continue
    logger.warning("[execute] 工具参数解析失败，按空参调用: %s", tool_spec)
    return {}, False


_DISPLAY_CREATE_PROMPT = """\
你是 OLED 屏幕文案创作器。结合最近这句对话，为看板娘生成一句要显示在访客
IoT 设备小屏幕上的一句话（30 字以内，温暖、应景、口语化，可带一点猫系口癖，
不需要称呼和标点堆砌）。只输出文字本身，不要任何解释、引号或前缀。
最近对话：{user_msg}
页面上下文：{page_ctx}"""


def _create_display_text(user_msg: str, page_ctx: str) -> str:
    """device_oled_display 缺 text 时的屏幕文案创作（execute 内唯一创作点）。

    小模型 + 短输出 + 短超时；失败兜底一句通用文案（屏幕显示是即时演示动作，
    兜底文案无事实风险）。创作结果打 trace（与调用参数同窗，事后可查屏上
    到底写了什么）。
    """
    fallback = "主人来看我啦，今天也要开心喵～"
    try:
        llm = get_llm(temperature=0.7, max_tokens=80, timeout=20, enable_thinking=False)
        resp = llm.invoke(_DISPLAY_CREATE_PROMPT.format(
            user_msg=user_msg[-200:], page_ctx=page_ctx[:200]))
        text = (getattr(resp, "content", str(resp)) or "").strip().strip("\"'“”‘’")
        if not text:
            return fallback
        record("execute", "display_create", text=text[:80])
        return text
    except Exception as e:
        logger.warning("[execute] 屏幕文案创作失败，用兜底文案: %s", e)
        return fallback


_VERDICT_PASS, _VERDICT_BLOCK = "PASS", "BLOCK"


def _check_spec(name: str, args: dict, args_ok: bool, raw: str, skill: str) -> tuple[str, str]:
    """checker 确定性验收（20260904，execute 循环内逐 spec 调用，无 LLM）。

    输入 = spec 实际调用值（args 是文案注入后值）+ 工具原始返回。只做回执形态
    校验（错误帧/空结果/命令帧形状），不做文本语义判断——语义由 planner 从帧
    里自己读（错误修正重试是 planner rule5 的活）。
    PASS → 该执行成为系统确认事实（receipts，跨轮执行记忆原料）；
    BLOCK → 该执行不进回执（错误结果不是事实），进 blocked 交 planner/reflector。
    """
    if name not in _TOOL_MAP:
        # 白名单结构上到不了 execute，防御保留（执行器不静默吞越权）
        return _VERDICT_BLOCK, "unknown_tool"
    if not args_ok:
        return _VERDICT_BLOCK, "args_parse"
    text = raw or ""
    if not text.strip():
        return _VERDICT_BLOCK, "empty_result"
    if text.lstrip().startswith("__ERROR__"):
        return _VERDICT_BLOCK, "error_frame"
    # 命令工具契约层校验：动作工具必须返回命令帧（工具返回形态漂移 = 执行未
    # 按契约发生，如 navigate 返回了纯文本而非 NAVIGATE:/AUTO_NAVIGATE:）。
    # device_oled_display 的"未在 5s 内回执确认"属软失败（指令确已下发），判
    # PASS——如实告知场景，不把软失败升成受阻链。
    if name == "navigate_to" and not text.startswith(("NAVIGATE:", "AUTO_NAVIGATE:")):
        return _VERDICT_BLOCK, "cmd_shape"
    if name == "toggle_effect" and not text.startswith("EFFECT:"):
        return _VERDICT_BLOCK, "cmd_shape"
    if name == "toggle_dark_mode" and not text.startswith("DARKMODE:"):
        return _VERDICT_BLOCK, "cmd_shape"
    return _VERDICT_PASS, "ok"


def execute_node(state: AgentState, config: RunnableConfig | None = None) -> dict:
    """确定性执行 planner 调用清单：逐条 literal_eval 参数 → _TOOL_MAP 调用 →
    ToolMessage 帧（含 __ERROR__ 错误帧）→ 逐 spec checker 验收（PASS 回执 /
    BLOCK 受阻）→ 回 planner（受阻首现）或 reflector（同 spec 二次受阻）。

    20260827 实测教训保留：工具执行前做断连检查——写操作（设备指令下发/导航/
    特效切换）绝不发生在用户已离开之后。
    执行器无自由意志因此也无越权通道：planner 决策经 instantiate_plan 白名单
    （_EXPLICIT_TOOLS/_CALLABLE_QUERY_TOOLS/技能模板）生成，execute 照单全收；
    与旧 tools_node 的差异 = 没有"model 自拟参数""计划外调用授权拒绝"分支——
    那些自由在 20260903 已从执行层移除（用户裁决）。20260904：checker 是
    确定性验收函数（读回执形态），不新增决策权——执行层仍零自由。
    """
    if _stopped(config):
        logger.info("[execute] cancelled (client disconnected) — 不执行任何工具（含写操作）")
        raise AgentCancelled()
    plan = parse_plan(state.get("plan", ""))  # 缺 plan 容错 → chat 兜底（tools 空，零调用）
    specs = plan["tools"]
    if not specs:
        return {"messages": []}
    executed = state.get("executed") or []
    user_msg = _last_user_msg(state["messages"])
    page_ctx = _page_ctx(state["messages"])
    results: list = []
    receipts = list(state.get("receipts") or [])  # 请求内累计（与 executed 同模式）
    blocked: list = []                            # 只含本轮受阻项（路由/reflector 用）
    prev_seen = set(state.get("blocked_seen") or [])  # 本轮之前的受阻 spec 集
    for idx, spec in enumerate(specs):
        name = _tool_name(spec)
        args, args_ok = _tool_args(spec)
        tool = _TOOL_MAP.get(name)
        # 屏幕文案创作：text 参数缺失/为空 → execute 结合对话创作（技能固有设计）
        if name == "device_oled_display" and not args.get("text"):
            args = dict(args)
            args["text"] = _create_display_text(user_msg, page_ctx)
        _t_tool = time.monotonic()
        if tool is None:
            out = f"__ERROR__: 未知工具 {name}（planner 调用清单越界，被 execute 拒绝执行）"
            logger.warning("[execute] 未知工具 %s，拒绝执行", name)
        else:
            try:
                out = tool.invoke(args)
            except Exception as e:
                out = f"__ERROR__: {type(e).__name__}: {e}"
        results.append(ToolMessage(
            content=str(out), tool_call_id=f"execute_{idx}", name=name))
        logger.info("[execute] %s(%s) → %.100s", name, json.dumps(args, ensure_ascii=False),
                    str(out))
        result_ = str(out)
        # rag_search 完整落盘（行式候选已精简）——事后可分析完整候选与选择
        # 对比，不必翻代码复现截断（20260831 事故复盘教训）
        if name != "rag_search":
            result_ = result_[:200]
        record("execute", "call", name=name, args=args,
               duration_s=round(time.monotonic() - _t_tool, 3), result=result_)
        # checker 确定性验收（20260904）：PASS → 回执（系统确认事实，跨轮执行
        # 记忆与 reflector 的原料）；BLOCK → 受阻项（不进回执——错误结果不是
        # 事实）。args 是文案注入后值（device_oled_display 回执须能呈现实际屏文）。
        verdict, reason = _check_spec(name, args, args_ok, str(out), plan["skill"])
        if verdict == _VERDICT_PASS:
            receipts.append({"skill": plan["skill"], "tool": name,
                             "args": {k: str(v)[:200] for k, v in args.items()},
                             "result": str(out)[:200], "ts": time.time()})
        else:
            blocked.append({"spec": spec, "tool": name, "reason": reason,
                            "result": str(out)[:300]})
        record("execute", "check", tool=name, verdict=verdict, reason=reason,
               skill=plan["skill"])
    repeat = any(b["spec"] in prev_seen for b in blocked)  # 同 spec 二次受阻 = 重试已败/链断
    updates = {"messages": results,
               "executed": executed + [s for s in specs if s not in executed],
               "receipts": receipts, "blocked": blocked,
               "blocked_seen": sorted(prev_seen | {b["spec"] for b in blocked}),
               "blocked_repeat": repeat}
    if not blocked:
        updates["issues"] = ""  # 全 PASS → 复盘建议清空（不残留误导下一轮 planner）
    return updates


# ---------------------------------------------------------------------------
# reflector 节点：受阻执行复盘（20260904 新增；取代旧 reflector 的仅存职责）
# ---------------------------------------------------------------------------
# 旧 reflector（20260824-20260903）死于 LLM 读叙述文本质检：1.26 截断误杀、
# 1.30 误杀正确链、1.32 采信模型自称、REVISE 被无视、预算耗尽静默 accept——
# 用户裁决把自由度从执行层收走（planner-authority），reflector 整体废除。
# 20260904 重构让 checker 确定性验收回执，reflector 以极小预算回归唯一合理
# 职责：execute 同 spec 二次受阻（rule5 首轮改参重试已败/依赖链断）后的复盘。
# 与老 reflector 的三个结构性差异：
#   1. 输入无散文——图序 execute→reflector 先于 model，叙述尚未生成、结构上
#      看不到（检查对象是受阻项/回执/帧，不是叙述文本）；
#   2. 输出两行契约（ISSUE:/DECIDE:），replan 只把 ISSUE 给 planner 当修正
#      指引（planner 仍是唯一决策点），wrap_up/预算耗尽 → 确定性收尾计划；
#   3. 预算 REFLECT_MAX_ROUNDS=2 硬顶 + 解析失败一律 wrap_up 兜底——LLM 复盘
#      循环不失控，到顶即终局（无静默 accept，gate 照常检查终局轮叙述）。

_REFLECTOR_PROMPT = """\
你是执行受阻复盘器——纯诊断角色：不执行任何工具、不改写计划、不评价叙述。
输入：上一轮执行计划（含 TODO 后续依赖链声明）、checker 受阻项（验收未通过 =
执行没按契约发生）、工具帧与已验收回执（修正参数的唯一真实来源）。

判定规则：
1. 逐项诊断受阻项（spec=工具+参数 / reason=受阻原因 / result=工具返回）：
   - 缺的值（article_id/路径/设备名/关键词等）能在"工具帧与已验收回执"里找到
     → ISSUE 指出该受阻项缺什么、用哪个真实值怎么改（只能引用输入中出现的
     真实值，不新造）；
   - 受阻原因是工具不可用/参数无法修正/缺的值任何输入都没有 → 如实说明差
     什么，判 wrap_up——系统没有额外取证通道，编造修正方案 = 二次幻觉。
2. 输出严格两行，不要任何其他文字（ISSUE 单行 ≤150 字，多项用分号分隔）：
ISSUE: <受阻项 → 缺什么 → 怎么改>
DECIDE: replan|wrap_up
3. 禁区：不评价叙述质量（本轮叙述尚未生成、你也看不到）；不引用记忆印象中
   的 id/路径/设备名/页面；不虚构工具或修正方案。

[执行计划]
{plan}

[本轮受阻项]
{blocked}

[工具帧与已验收回执]（≤900 字）
{frames}"""


def reflector_node(state: AgentState, config: RunnableConfig | None = None) -> dict:
    """复盘受阻执行（20260904）：同 spec 二次受阻后路由至此（route_after_execute），
    由复盘 LLM 判 ISSUE+DECIDE，输出只驱动两种去向——replan 把修正指引给
    planner（唯一决策点不变），wrap_up/预算耗尽走确定性收尾计划 + reflect_end
    → model 叙述、gate 照常检查。复盘不计入 plan_rounds（planner 轮次上限语义
    不变），只占 REFLECT_MAX_ROUNDS 次复盘预算。
    """
    if _stopped(config):
        logger.info("[reflector] cancelled (client disconnected)")
        raise AgentCancelled()
    rounds = state.get("reflect_rounds", 0)
    blocked = state.get("blocked") or []
    has_frames = _has_frames(state["messages"])

    def _terminal(reason: str, new_rounds: int) -> dict:
        """确定性收尾计划 + reflect_end → model。记录后无 LLM，绝不静默 accept。"""
        plan_obj = _terminal_plan(has_frames, reason)
        logger.info("[reflector] 终局收尾（%s）", reason)
        return {"plan": plan_encode(plan_obj), "issues": "",
                "reflect_rounds": new_rounds, "reflect_end": True}

    if rounds >= REFLECT_MAX_ROUNDS or not blocked:
        reason = ("复盘轮次已达上限" if rounds >= REFLECT_MAX_ROUNDS
                  else "没有可复盘的受阻项")
        record("reflector", "terminal", reason=reason, round=rounds)
        return _terminal(f"受阻项复盘已达上限（{REFLECT_MAX_ROUNDS} 次）仍无解",
                         rounds)

    plan_txt = (state.get("plan") or "")[:400]
    blocked_txt = "\n".join(
        f"- {b.get('spec', '')} | reason={b.get('reason', '')}"
        f" | result={str(b.get('result', ''))[:150]}"
        for b in blocked[:6])[:600] or "（空）"
    facts = (_frame_texts(state["messages"]) + "\n"
             + _receipts_text(state.get("receipts") or []))[:900]
    record("reflector", "round_start", blocked=[b.get("spec") for b in blocked],
           round=rounds + 1)
    _t0 = time.monotonic()
    try:
        # 复盘是确定性诊断（判 replan/wrap_up 两值）——最低温 + 短输出 + 无思考
        llm = get_llm(temperature=0.0, max_tokens=300, timeout=30,
                      enable_thinking=False)
        resp = llm.invoke(_REFLECTOR_PROMPT.format(
            plan=plan_txt, blocked=blocked_txt, frames=facts))
    except Exception as e:
        logger.warning("[reflector] LLM 异常，按收尾终局: %s", e)
        record("reflector", "terminal", reason="llm_error", round=rounds + 1)
        return _terminal("受阻复盘 LLM 异常，按已验收执行如实收尾", rounds + 1)
    dur = time.monotonic() - _t0
    logger.info("[reflector] LLM 复盘耗时=%.1fs（round %d/%d）",
                dur, rounds + 1, REFLECT_MAX_ROUNDS)
    raw = (getattr(resp, "content", str(resp)) or "").strip()
    dm = re.search(r"DECIDE\s*[:=]\s*(\w+)", raw, re.IGNORECASE)
    decide = dm.group(1).lower() if dm else "wrap_up"  # 解析失败 → wrap_up 兜底
    im = re.search(r"ISSUE\s*[:=]\s*(.+)", raw, re.IGNORECASE | re.DOTALL)
    issue = ""
    if im:
        issue = im.group(1).strip().split("\n")[0].strip()[:300]  # 契约单行
    record("reflector", "verdict", blocked=[b.get("spec") for b in blocked],
           decide=decide, issue=issue[:120], round=rounds + 1)
    if decide == "replan" and issue:
        # ISSUE 注入 state.issues → 下一轮 planner 提示词复盘建议区；路由回 planner
        logger.info("[reflector] replan → planner 按 ISSUE 重规划: %s", issue[:120])
        return {"issues": issue, "reflect_rounds": rounds + 1, "reflect_end": False}
    logger.info("[reflector] wrap_up → 确定性收尾: %s", issue[:120] or "无可用修正")
    return _terminal(f"受阻复盘判定收尾（{issue[:100] or '无可用修正'}）", rounds + 1)


# ---------------------------------------------------------------------------
# model 节点：零工具的 narrator（取代旧 ReAct executor）
# ---------------------------------------------------------------------------
# 20260903 架构：model 不再 bind_tools——LLM 结构上不可能发出 tool_calls，
# "执行器不听 planner"的旧根因（模型自选工具/自拟参数/跳过检索直接答）从
# 模型侧连通道都没有。model 的唯一职责：把 execute 的工具帧 + 页面上下文 +
# 计划契约组织成给访客的最终回复（narrator）。叙述纪律见 _EXECUTOR_PROMPT。

_EXECUTOR_PROMPT = """\
{persona}

[执行计划]（系统决策结果——本轮执行了什么、按什么契约回复）：
{plan}

[本轮工具执行记录]（站内事实的唯一来源，逐字依据，不要扩展）：
{tool_frames}

[本轮执行回执]（系统确定性验收通过的实际执行事实——含工具参数与返回，
如实转述的依据；为空 = 本轮没有已验收的执行）：
{exec_receipts}

当前页面上下文（前端实时上报的访客位置/特效/夜间模式，以此为准）：
{page_ctx}

情绪表达素材（20260904：真正的情绪表达时才引用，不堆砌不机械）：
{sticker_guide}

叙述纪律（你是回复者，不是执行者）：
1. 你没有任何可以直接调用的工具。站内查询、跳转、特效/夜间切换、设备操作都
   由系统在上面的执行计划中完成——你只负责把"工具执行记录"里的返回组织成回复。
2. 引用站内内容（文章/说说/留言/公告/页面/链接/细节）时：只能来自"工具执行记录"
   或页面上下文。记录里没有的内容（标题/细节/数字/URL/是否存在）一律不得编造。
3. "工具执行记录"为"（本轮尚无工具执行）"时：本轮没有执行过任何查询/动作——
   不得声称查过、读过、搜过、翻找过、打开过、跳转过、显示过（口语换说法也算
   声称：如"去站内翻找了一圈""把博客扫了一遍"）；站内问题如实说明无法确认，
   或建议用户稍后再问。
4. 被访客质疑某操作是否真的执行过（"你确定？""真有这个页面吗？"）：
   - 记录里有对应工具返回 → 如实转述该返回（含失败/错误信息），不扩大不粉饰；
   - 记录里没有对应执行 → 如实承认"我这边没有看到这次操作的执行记录，刚才
     好像没有真正执行"，绝不圆场说"其实已经做了"。
   - 跨轮记忆（页面上下文 recent_executions=，20260904）与"本轮工具执行记录/
     本轮执行回执"同为准绳：转述执行事实（含上轮/历史轮的实际屏文/路径/开关
     状态）以三者为准，三者之外的执行声称（"我记得好像显示过"）不得出口。
5. 工具返回以 __ERROR__ 开头 → 如实转述失败原因，不把失败说成成功、不声称
   已完成。执行计划 NOTE 要求如实告知的（页面不存在/已下线）照做。
6. 回复正文绝不输出 NAVIGATE:/AUTO_NAVIGATE:/EFFECT:/DARKMODE: 等命令前缀文本，
   也不要用伪工具调用格式表演执行过程。执行计划里的 TODO/过程注记是系统内部
   规划信息，不要复述。
7. 需要给出站内链接时，只能用"工具执行记录"或页面上下文里真实出现的地址，
   不确定就不要给。
8. 纯闲聊与博客内容无关的问题自由回答，但纪律 2/3/6 仍然适用。
9. 回复遵循计划 REPLY 行的契约组织。
10. 教访客操作本站页面/功能（怎么留言/放河灯/发说说/找什么按钮）时：只能讲
    "页面上下文"里注入的操作指南或工具返回里的真实内容；没有指南且没查到 → 如实
    说"站内没有使用说明，具体入口我也不确定"，禁止用一般网站/论坛经验脑补具体
    UI（输入框长什么样、填写项、提交/登录入口等）——脑补的 UI 细节即使"常识上
    合理"也是编造。
11. 访客重复提问（与对话历史里已问过的问题相同或高度相似，含原句重发）：
    绝不把历史里自己的回复原文再输出一遍——先点破重复（"这个问题你刚才
    问过啦～"），压缩成两三句要点重述（不复读全文、不重复举例/收尾句），
    再追问一句新意图；只有本轮工具查证带回与上轮不同的新事实时才重新完整
    叙述。页面上下文带 repeat_ask_note= 指示时按指示执行。
12. 叙述以讲清楚为准：对**有依据**的内容（工具返回、页面上下文、闲聊常识、
    人设知识）要展开充分——该给的背景、步骤、细节、例子、对比讲透，让访客
    一次看明白，不为"短"而刻意缩话；无依据的部分仍按纪律 2/3/4 处理（不
    编造、如实说不知道）。例外：纪律 11 的重复提问场景按 11 压缩重述。
13. 关键项标重点（20260905 访客反馈"不标重点"）：回复并列列举站内板块/功能/
    能力/技能（≥3 项）时，每项名称用 **加粗** 标出（如 **搜索文章**、**河灯
    留言**、**夜间模式**），可按项分行排列，让访客扫读即抓住要点；单句问答
    与连续正文段落不强行加粗。"""


def model_node(state: AgentState, config: RunnableConfig | None = None) -> dict:
    """narrator：零工具回复节点（人设 + 计划 + 工具帧 + 叙述纪律 → 最终回复）。

    与旧 ReAct executor 的本质区别：不 bind_tools（无 tool_calls 输出通道）、
    不背执行责任（执行是 execute 的活）——模型只把已发生的事实说清楚。
    这正是"执行器不听话"事故的结构性解：模型想"自由发挥执行"也无处发挥。
    """
    if _stopped(config):
        logger.info("[model] cancelled (client disconnected)")
        raise AgentCancelled()
    # enable_thinking=False（20260831 用户拍板）：thinking 模式在长上下文（工具
    # 结果全文 + 检索候选 + 历史）下思考链爆炸——慢调用监控 3 条 model WARN
    # （46.8s/79.1s/105.8s）+ 20260830 超时事故（118s/146.9s）同源。生成质量
    # 由 golden 全量回归把关。
    llm = get_llm(enable_thinking=False)  # 主模型：对话生成（温度 0.7、可流式）
    system = SystemMessage(content=_EXECUTOR_PROMPT.format(
        persona=BLOG_ASSISTANT_PROMPT, plan=state["plan"],
        tool_frames=_frame_texts(state["messages"]),
        exec_receipts=_receipts_text(state.get("receipts") or []),
        page_ctx=_page_ctx(state["messages"]),
        sticker_guide=STICKER_GUIDE))
    _t0 = time.monotonic()
    logger.info("[model] LLM 调用开始（narrator）")
    record("model", "llm_start")
    resp = llm.invoke([system] + state["messages"])
    dur = time.monotonic() - _t0
    slow = dur > 30
    (logger.warning if slow else logger.info)(
        "[model] LLM %s（narrator）耗时=%.1fs", "慢调用" if slow else "完成", dur)
    record("model", "llm_done", duration_s=round(dur, 2),
           **({"slow": True} if slow else {}))
    return {"messages": [resp]}


# ---------------------------------------------------------------------------
# gate 节点：唯一确定性检查（取代旧 reflector 的 9 闸 + LLM 质检 + REVISE）
# ---------------------------------------------------------------------------
# 20260903 架构：执行正确性不再需要检查（execute 是确定性执行器，planner 是
# 唯一决策源——"工具没按计划调"在结构上不存在）。gate 只兜两件事：
#   1. 叙述失真：narrator 文本声称 ≠ 帧事实（声称有执行但无帧 / 帧失败却说成功 /
#      确认式导航却说已到达 / 编造资源 URL / 正文混入命令前缀 / 空回复）；
#   2. 计划注记不遵守：NOTE 明示页面不存在/已下线时回复没有如实说明。
# 判定结果只有两种：通过 → done=True 收尾；不通过 → validate→fallback 直接
# 收尾（fallback 文本是给访客的如实回复，取代原回复，无 REVISE 重考轮——
# "打回重来"的纠错循环 20260903 已废除：检查不通过说明 narrator 不可信，
# 重考一轮只是再给它一次编的机会，确定性文本收尾更诚实也更省）。

# 回复中的资源 URL（/api/ 路径、图片资源）必须逐字出现在工具返回或用户消息
# 中（机器串，模型不会改写，逐字校验无假阴性）。代码块内 URL 不校验（教程/
# 示例场景）；裸域名/站内页路径引用（/about、/article/15 作建议链接）非资源
# 声称不校验——旧"编造文章链接"事故已由导航确定性快道 + planner 字面路径
# 校验结构性覆盖（链接只能来自 NAV_MAP/工具返回/用户消息，narrator 无编链
# 通道），此处只兜图片/API 资源地址。
_RESOURCE_URL_RE = re.compile(
    r"/api/[^\s)\]\"'<>，。、；：`*|）]+|https?://[^\s)\]\"'<>，。、；：`*|）]+\.(?:jpe?g|png|webp|gif|svg)")


def _url_trusted(u: str, messages: list) -> bool:
    """资源 URL 是否逐字出现在工具返回/用户消息（绝对 URL 先归一化为 path）。"""
    trusted = "\n".join(_msg_text(m) for m in messages
                        if isinstance(m, (HumanMessage, ToolMessage)))
    if u in trusted:
        return True
    m = re.match(r"https?://[^/]+(/.*)$", u)
    return bool(m and m.group(1) in trusted)


def gate_node(state: AgentState, config: RunnableConfig | None = None) -> dict:
    """确定性检查节点：核对 narrator 叙述与帧事实/计划注记的一致性后收尾。

    有问题的轮次直接产出 fallback 收尾（done=True + [Fallback 决定] 消息 +
    fallback_text），server.py 据此把最终回复替换为 fallback 文本。
    """
    if _stopped(config):
        logger.info("[gate] cancelled (client disconnected)")
        raise AgentCancelled()
    _t0 = time.monotonic()
    plan = parse_plan(state.get("plan", ""))
    msgs = state["messages"]
    frames = [m for m in msgs if isinstance(m, ToolMessage)]
    last_ai = next((m for m in reversed(msgs) if isinstance(m, AIMessage)), None)
    reply = ((getattr(last_ai, "content", "") or "").strip() if last_ai else "")
    record("gate", "check", skill=plan["skill"], frames=len(frames))

    # ── 1. 空回复（narrator 没说出话）→ fallback ─────────────────────────
    if not reply:
        return _fallback_result("empty_reply", _FALLBACK_EMPTY, plan, len(frames))

    # ── 2. 命令前缀文本（任何轮次，正文出现命令帧前缀 = 假装发命令）─────────
    # ── 3. 编造资源 URL（任何轮次，工具返回/用户消息中不存在的 /api 或图片）──
    issue = _claim_issue(reply, plan["skill"], plan, bool(frames))
    if issue:
        return _fallback_result(*issue, plan, len(frames))
    code_stripped = re.sub(r"```.*?```", "", reply, flags=re.S)
    fabricated = [u for u in _RESOURCE_URL_RE.findall(code_stripped) if not _url_trusted(u, msgs)]
    if fabricated:
        logger.info("[gate] URL 声称无依据：%s", "、".join(fabricated[:3]))
        return _fallback_result("fabricated_url", _FALLBACK_URL, plan, len(frames))

    if not frames:
        # ── 4. 零工具轮（计划 TOOLS 为空）───────────────────────────────
        # 动作技能（navigate）零工具 = NOTE 明示不存在/已下线（instantiate_plan
        # 的注记路径）→ 核验回复如实措辞；chat/content_query 零工具声称检查
        # 已在 _claim_issue 处理。
        if plan["skill"] == "navigate" and "不调用任何工具" in plan["note"]:
            if "已下线" in plan["note"]:
                honest = any(k in reply for k in _HONEST_DOWN)
                fb = _FALLBACK_DOWN
            else:
                honest = any(k in reply for k in _HONEST_GONE)
                fb = _FALLBACK_GONE
            if not honest:
                logger.info("[gate] 零工具注记但未如实告知 → fallback（navigate）")
                return _fallback_result("not_honest", fb, plan, 0)
        record("gate", "pass", zero_frame=True,
               duration_s=round(time.monotonic() - _t0, 2))
        logger.info("[gate] PASS（零工具轮，skill=%s）", plan["skill"])
        return {"done": True}

    # ── 5. 有帧轮：帧内容与叙述的一致性兜底 ──────────────────────────────
    tool_text = "\n".join(str(getattr(m, "content", "")) for m in frames)
    err_frames = [f for f in frames
                  if str(getattr(f, "content", "")).lstrip().startswith("__ERROR__")]
    # 5a. 工具失败（__ERROR__ 帧）却回复完成式声称 → 把失败说成成功
    #     （回复含失败类实词则不触发——如实报告失败是正当行为）
    if err_frames and not any(k in reply for k in
                              ("失败", "错误", "出错", "未成功", "不成功", "没成功", "还是不行")):
        if _COMPLETION_CLAIM_RE.search(reply):
            logger.info("[gate] 工具帧 __ERROR__ 但回复含完成式声称 → fallback")
            return _fallback_result("err_frame_claim", _FALLBACK_ERR_CLAIM, plan, len(frames))
    # 5b. 确认式导航（NAVIGATE: 帧、无 AUTO_NAVIGATE:）却回复到达声称 →
    #     页面实际未跳转（前端等确认）
    if plan["skill"] == "navigate" and "NAVIGATE:" in tool_text and "AUTO_NAVIGATE:" not in tool_text:
        if _NAV_ARRIVAL_RE.search(reply):
            logger.info("[gate] NAVIGATE 确认帧 + 到达声称 → fallback")
            return _fallback_result("nav_pending_claim", _FALLBACK_NAV_PENDING, plan, len(frames))

    record("gate", "pass", zero_frame=False, frames=len(frames),
           duration_s=round(time.monotonic() - _t0, 2))
    logger.info("[gate] PASS（skill=%s frames=%d）", plan["skill"], len(frames))
    return {"done": True}


# ---------------------------------------------------------------------------
# 4. Edge：条件边 —— 路由逻辑（循环/终止都在这）
# ---------------------------------------------------------------------------

def route_after_planner(state: AgentState) -> Literal["execute", "model"]:
    """planner 决策完：
      - 计划有调用清单（TOOLS 非空）→ 去 execute 确定性执行
      - 收尾轮（TOOLS 空：chat/信息已足够/查无结果）→ 直接去 model 叙述
    """
    plan = parse_plan(state.get("plan", ""))
    return "execute" if plan["tools"] else "model"


def route_after_execute(state: AgentState) -> Literal["planner", "reflector"]:
    """execute 执行完的下一站（20260904 checker 驱动路由）：
      - 本轮无受阻项 → planner（正常多轮循环：看工具返回再决策，现状不变）
      - 有受阻项但都是首现（planner rule5 的合法改参重试空间，零新增 LLM）→
        planner 按错误修正重试
      - blocked_repeat（受阻 spec 此前已受阻过 = 首轮重试已败/依赖链断）→
        reflector 复盘（≤2 次 LLM），不再让 planner 盲试第三遍
    """
    if not state.get("blocked"):
        return "planner"
    if state.get("blocked_repeat"):
        return "reflector"
    return "planner"


def route_after_reflector(state: AgentState) -> Literal["planner", "model"]:
    """reflector 复盘完：
      - DECIDE=replan → planner（state.issues = ISSUE 修正指引，planner 仍是
        唯一决策点，按建议重试不越权）
      - reflect_end（wrap_up/复盘预算耗尽/LLM 异常/无受阻项防御）→ model 叙述
        （plan 已被确定性收尾计划替换，narrator 据已验收回执如实叙述）
    """
    return "model" if state.get("reflect_end") else "planner"


# ---------------------------------------------------------------------------
# 5. 组装与编译
# ---------------------------------------------------------------------------

def build_graph():
    """构建手写图：节点 + 边 + 编译。返回 CompiledStateGraph。

    拓扑（20260904 定稿：planner 全权 + checker 确定性验收 + 受阻分流）：
      START → planner ─┬─ 有调用清单 → execute（逐 spec：执行 + checker 验收）
                       │                 └─ route_after_execute
                       │                    ├─ 无受阻/受阻首现 → planner
                       │                    │   （多轮循环，上限 4；首现受阻 =
                       │                    │    rule5 改参重试，零新增 LLM）
                       │                    └─ 重复受阻 → reflector（复盘 ≤2 次）
                       │                         ├─ replan → planner（ISSUE 指引）
                       │                         └─ 终局 → model（确定性收尾计划）
                       └─ 收尾轮 → model（narrator）→ gate → END

    planner ⇄ execute 是主循环（决策-执行交替）；reflector 只在重复受阻的罕见
    异常路径介入（小预算复盘，不复活老 LLM 质检）；model/gate 各走一次收尾。
    """
    g = StateGraph(AgentState)

    g.add_node("planner", planner_node)
    g.add_node("execute", execute_node)
    g.add_node("reflector", reflector_node)
    g.add_node("model", model_node)
    g.add_node("gate", gate_node)

    g.add_edge(START, "planner")
    g.add_conditional_edges("planner", route_after_planner,
                            {"execute": "execute", "model": "model"})
    g.add_conditional_edges("execute", route_after_execute,
                            {"planner": "planner", "reflector": "reflector"})
    g.add_conditional_edges("reflector", route_after_reflector,
                            {"planner": "planner", "model": "model"})
    g.add_edge("model", "gate")
    g.add_edge("gate", END)

    return g.compile()


def graph_input(messages: list) -> dict:
    """图输入构造：state 形状归本模块管，调用方（server.py）不手写字段。

    planner 节点会立刻写入 plan/plan_rounds/done，这里给空初值只为了让输入
    形状完整、可读。
    """
    return {"messages": messages, "plan": "", "plan_rounds": 0, "done": False,
            "executed": [], "receipts": [], "blocked": [], "blocked_seen": [],
            "blocked_repeat": False, "reflect_rounds": 0, "issues": "",
            "reflect_end": False}
