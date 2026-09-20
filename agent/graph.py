"""手写 LangGraph 图 —— Agent 核心重写（20260903 架构裁决：planner 全权）

替代 create_agent 黑盒：显式声明 planner / execute / model / gate 节点与状态流转。

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

纯函数层已外移（20260912 拆分）：agent/context.py = 上下文/帧文本组装；
agent/decisions.py = 确定性决策层（快道 / 意图扫描 / 候选裁决 / 终局计划）。
本文件只留图拓扑与节点编排（planner/execute/reflector/model/gate + 路由）。

LangGraph 四件套：
  State  —— AgentState（节点间共享的字典，字段决定"工作台长什么样"）
  Node   —— planner/execute/model/gate（每个是普通函数：state 进、更新字段出）
  Edge   —— 普通边（顺序传送带）+ 条件边（按返回值路由，循环/终止所在）
  Reducer—— Annotated[list, add_messages]：messages 字段"追加"而非覆盖

与现有工程外壳的关系（全部保留不动）：
  _build_messages（历史/摘要注入/时间锚）、SSE 帧协议、超时体系、recursion_limit
  —— 都在 server.py，本文件只负责"图长什么样"。
  注：_force_display 强制路由已随 20260828 影子系统重构移除（见问题记录）。
"""


# ⚠️ 本文件**不要**加 `from __future__ import annotations`（20260920 实测踩过）：
# 它把注解变成字符串，而 langgraph 是靠 `p.annotation in (RunnableConfig, RunnableConfig | None)`
# **对象比较**来判断"第二个参数是不是 config"的（langgraph/_internal/_runnable.py）。
# 字符串注解比对不上 ⇒ 节点被当成只收 state 调用 ⇒ `config` 静默取默认值 None，
# 于是 `_stopped(config)` 恒为 False（断连中断在节点内失效）、principal 恒为 UNKNOWN。
# 没有报错、没有异常，只有一条 UserWarning（生产日志里根本不会被看见）。
# test_authz.py 用 `warnings.simplefilter("error")` 构建图来锁这一条：注解一旦退回
# 字符串，套件立刻红。
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
from agent import authz
from agent.context import (GUESTBOOK_GUIDE, SITE_GUIDE, _attach_page_guide,
                           _doc_anchors, _frame_texts, _has_frames, _last_user_msg,
                           _msg_text, _page_ctx, _receipts_text, _recent_tail,
                           _short_reply_hint)
from agent.decisions import (MAX_PLAN_ROUNDS, _any_error_frame, _article_fast_path,
                             _candidate_detail_plan, _display_fast_path, _doc_title,
                             _effect_switch_fast_path, _intent_done, _intent_hints,
                             _nav_fast_path, _scan_action_intents, _search_terms,
                             _terminal_plan, _title_relevant, _tool_name, _wrap_up_plan)
from agent.entities import receipt_digest
from agent.principal import UNKNOWN as UNKNOWN_PRINCIPAL
from agent.prompts import BLOG_ASSISTANT_PROMPT, STICKER_GUIDE
from agent.refs import parse_data, ref_error_reason, ref_hints, resolve_args
from agent.skills import (FUZZY_NAV_RULES, NAV_MAP, SKILL_MAP,
                          _CALLABLE_QUERY_TOOLS_ORDER, build_planner_context,
                          instantiate_plan)
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


def _principal_of(config: RunnableConfig | None) -> "Principal":
    """本轮调用者身份（server.py 经 config 注入，见 agent/principal.py）。

    取不到 → UNKNOWN（零权限的占位，不是"管理员"）。单元的/dev 直调图、老路径
    请求都会走这一支；**是否据此拦截由 authz.enforcing() 决定**（默认 shadow）。
    """
    p = (config or {}).get("configurable", {}).get("principal")
    return p if p is not None and hasattr(p, "uid") else UNKNOWN_PRINCIPAL


# 工具一次构建全局复用（tools/base.py 的 @tool 都是纯函数，无状态）
_TOOLS = get_all_tools()
_TOOL_MAP = {t.name: t for t in _TOOLS}


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
    - tool_data:    请求内已执行工具返回的**结构化**值（[{tool,data}]，按执行顺序
                   累计）——参数引用（$tool[0].field，见 agent/refs.py）的取值
                   来源。与 receipts 的分工：receipts 是"系统验收过的事实"（给
                   narrator/跨轮记忆看），tool_data 是"下一步填参要用的数据"。
    - fallback_text: gate fallback 的如实用语（20260920 补声明）。**必须显式声明**：
                   LangGraph 只把 state schema 里声明过的 key 透出 updates 流，
                   未声明的 key 会被静默丢弃 ⇒ server.py 的 `upd.get("fallback_text")`
                   恒为假、`__RESET__` 永不发出、被 gate 否定的叙述照常展示并入库
                   （20260903 起 2.5 周实际失效，见 _fallback_result 注释）。
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
    tool_data: list[dict]
    fallback_text: str


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

用户消息里的动作意图清单（系统确定性扫描 + 按执行事实标注，每轮重算；扫描只是
提醒，该意图是否真实存在、是否该执行，以你的判断为准）：
{intent_hints}

本会话已点名文档（系统从对话历史与跨轮执行记忆里确定性提取的指代锚点——判断
"用户说的是哪一篇"时**先在这里对号入座**；已经在列的不必再检索去找）：
{doc_anchors}

{round_info}

{recent_context}

短应答提示（当前消息只是"要/好/不用了/算了"这类短应答时，这里给出它所承接的
上一轮泠月发言与判定方向；不是短应答则为缺省语）：
{short_reply_hint}

{tool_results}

本轮已执行工具的**可引用字段**（参数引用的取值来源，见规则 3b——字段名照抄，
路径只能从这里列出的键名前缀往下写，不许臆造）：
{ref_hints}

复盘建议（reflector 对重复受阻项的 ISSUE 修正指引——仅当上一轮复盘判 replan
后才有内容；没有则为缺省语，按常规规则决策）：
{reflector_feedback}

判定规则：
1. 决策类型（SKILL）：
   - **短应答先还原语义**：消息只是"要/好/可以/不用了/算了"这类短应答时（上方
     短应答提示会点明），它**不是新话题**——含义由上一轮泠月的发言决定：同意/
     要求继续 → 把泠月提议的那件事真的规划出来执行（该点名的工具照常点名），
     不得只口头答应；拒绝/收回 → 本轮零调用收尾，简短确认不做，不得再执行那个
     动作也不得声称做了什么。禁止拿短应答去检索或答别的内容。
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
   - 列表/数据型（最新留言/说说/公告、时间、站点信息/作者/备案号/社交链接、
     置顶文章、分类/标签等）→ PARAMS.tools 点名上方菜单里的无参数据工具；
     **禁止拿 search_notes/rag_search 去"绕"站点信息类问题**——检索索引只含
     文章正文，对站点元数据零命中，绕一圈只会得到空/无关结果并误答"站内没有"
     （20260913 实证：问社交链接，连读两篇无关文章后答"站内没有"，而链接一直
     在数据接口里）；问"有没有人聊过/写过 X"必须成对点名 list_guestbook 与
     list_talks 两个数据源；天气 → PARAMS.calls 给 get_weather(location)
   - "有没有/有哪些 X 相关文章"（主题列举）→ PARAMS.calls 必须成对点名两条：
     search_notes(核心词) + list_notes（page=1、page_size=50）——关键词搜
     正文 + 全量标题比对互补，缺一不可（正文措辞常与主题词不一致：问"嵌入式
     相关文章"，搜"嵌入式"只命中 Git 教程一处举例，真相关的是《ESP32-S3-OBC
     固件接入参考》《IoT 设备接入物联网平台指南》，标题含专名而关键词不含；
     只 search_notes 命中不足就收尾、或只 list_notes 不真检索，都不对）；
     两份返回交叉比对后再下"有/没有"的结论
   - 知识型/验证型 → PARAMS.calls 给定位调用。**定位之前先问一句：用户问的
     是不是"本会话已点名文档"里已经列出的那一篇？**是 → 直接按规则 4 ⓪ 用它
     的 id 读全文，本轮不需要任何检索（检索是给"上下文里没有的新主题"用的）。
     定位工具选型：
     机制/原理/做法型问题（"怎么实现/怎么工作/原理/机制/怎么做到/区别"）先
     rag_search 发用户原句语义定位——关键词 LIKE 会只命中标题含目标词的
     "问题记录"类文章（问"OTA 升级怎么实现"，语义检索第一是《ESP32-S3-OBC
     固件接入参考》OTA 章节，关键词却先命中《ESP32-S3 OTA 问题与解决记录》
     踩坑史）；事实/检索型（要数值/存在性/列举）→ search_notes 关键词：
     [{{"tool": "search_notes", "args": {{"keyword": "<用户原词或最小核心词>"}}}}]
     ——关键词=消息的信息核心词：剥掉称呼/问候/助词（例："小猫咪有没有嵌入式
     相关文章" → 关键词"嵌入式"，绝不是"小猫咪"），宁短勿长
     候选命中后下一轮 get_article_detail 读全文——article_id 只能取上一轮工具
     返回里的真实 id，绝不自己编 id。取 id 有两种写法，**优先用引用**（见 3b）：
     ① 你从工具返回帧里读出 id 后把它写成字面值；② 直接写参数引用让系统去取。
     机制型候选多篇时优先读「参考/接入/指南/
     实现」类文档；「问题与解决记录/踩坑/FAQ」类是经验记录，仅当确实记载所问
     事实时引用。
   - **超长文章按节补读**（20260920）：工具返回帧若标注"超单帧上限，已按小节节选"、
     文末还列了「以下小节尚未展开」，说明这一篇**只带回了前几节**。要回答的问题
     若落在未展开的小节里（或没展开的节标题正是用户所问），下一轮补一次 PARAMS.calls：
     `get_article_detail`，article_id 取该帧里的 noteKey（可写参数引用），
     section 写清单里的小节名或编号（如 "9" 或 "9. 部署与运维"）——一次读一节，
     读到能作答就收尾。**"只带回前几节"不等于"文章里没有"**：不得据此说"文档里
     没写"（这正是旧版无声截断留下的坑）；反过来，帧里已经展开的小节不要再读一遍。
   - 检索零结果应变（至多补一轮）：
     a) 换 rag_search 语义检索一次；仍无 → b) 关键词换用户原词的变体再
     search_notes 一次（中文词零结果时保留数字/字母试原文，如"测试4"→"test4"；
     去掉口语缀词）；仍无 → c) 收尾如实告知"站内没有找到"，不得用记忆硬答
   - 一轮只给当前步，execute 只执行 TOOLS 行。若本计划是多步链的中间一步
     （后续步骤依赖本轮结果：先检索定位 → 下一轮读候选全文；先 content_query
     找到文章 → 下一轮 navigate 跳转），在计划里追加一行 TODO: <剩余步骤列表，
     用 → 分隔>——只描述后续依赖链，不重复本轮已给的步骤；后续步骤的参数
     （article_id 等）只能等上一轮工具返回后填写，绝不预先编造。单步/收尾轮
     不写 TODO 行。
3b. **参数引用**（下一步的参数取值来自上一步工具返回时，一律优先用引用）：
   只要某个参数的**值来自本轮已执行工具的返回**，就把该参数写成引用字面量
   `$<工具名>[<序号>].<字段名>`，由系统在调用前取值填入——不要把值从返回帧里
   "读出来再抄一遍"，更不要凭印象编。例：
   [{{"tool": "get_article_detail", "args": {{"article_id": "$search_notes[0].noteKey"}}}}]
   规则：
   - 序号 = 该工具返回**列表的下标**（0 = 第一条候选）；工具返回单个对象时
     只能写 [0]。
   - 字段名只能取上方"可引用字段"里列出的键（照抄原样，含大小写）。
   - 引用的目标必须是**本轮已经执行过**的工具；同名单工具多轮执行以最近一次
     返回为准。
   - 写完引用后**不要**在回复里展示这段语法，也不要解释它——按正常计划输出
     即可。
   - 引用解析失败（工具没执行过、序号越界、字段不存在、返回不是结构化数据）
     时系统**不执行**该调用并回一条带原因的错误帧；按规则 5 改参数重试一次
     （改写成字面值或换正确字段），仍失败就如实收尾，不得声称成功。
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
   - 文章指代（**不限于显式指代词**：用户那句话只是对上文的追问/催读/深化也算
     指代——"你看了吗就说没写""你倒是看完给结论啊""继续讲那篇""那个快道呢"——
     此时目标文档由上文决定，不是新主题）解析顺序：
     ⓪ 先看上方"本会话已点名文档"：用户说的那篇在列 → **直接采用它的 id**
     （标了"已读过全文"就据它作答或重读；没标就 get_article_detail(该 id)）。
     这一步**不需要任何检索**——别为了"确认是哪一篇"再跑 search_notes/rag_search
     （20260919 实证：为找《架构文档》(19) 跑 rag_search，BM25 命中同主题的
     《文章向量空间图谱项目文档》(46)，被拦截器读全文，整轮跑偏）；
     ① 否则看当前页面是否就是文章页（current_url 是 /article/<id>）→ 以它为准；
     ② 否则看页面上下文 recent_executions 里最近读取的文章行（形如"MM-DD HH:MM
     读取文章 19《标题》"，行首时间是发生时刻）——限定词与标题对得上 → 用该
     id（重读或据此作答）；
     ③ 只有标题没有 id（列表里未见过 id）→ **list_notes(page=1, page_size=50)
     用标题实词做字面匹配**——**禁止拿用户原句的主题词去 rag_search**：语义检索
     按内容相似度排序，找"某一篇"必错成"同主题的另一篇"；限定词与已知文章对
     不上、或记录里没有 → 先按限定词的实词 content_query 定位，拿到帧内真实 id
     再读/再跳。**候选标题与限定词对不上号时不许拿 top 候选硬读顶上**（读错一篇
     会把后续几轮全部带偏）——如实说候选里没有对得上的那篇
5. 多轮收敛：
   - **收尾前先核对上方动作意图清单**：一句话里有多个动作（"帮我把樱花打开，
     顺便切一下夜间模式"）时，跨技能动作一轮只能做一个——逐个做完是正常的多轮
     路径，不是异常；清单里还有【未完成】项就**不得收尾**（只做一半＝用户的
     要求被丢掉），下一轮继续规划该动作，全部【已执行】才允许收尾
   - 已执行动作技能（navigate/effect/darkmode/device_display/device_query/
     read_article）、工具返回已可见、**且意图清单已无未完成项** → 本轮收尾
     （chat 或 content_query 留空），绝不重复规划同款调用——动作已由工具帧完成，
     回复层会基于帧确认
   - 上一轮工具返回以 __ERROR__ 开头 → 按错误修正参数重试一次；已重试过或
     无法修正 → 收尾如实告知失败，不得声称成功
   - 上方复盘建议存在（reflector ISSUE，指明受阻项缺什么/怎么改）→ 按建议
     重试该修正；按建议执行后仍受阻 → 不再第三次自试，收尾如实结束——复盘
     建议是对已受阻项的修正指引，不是无限重试授权
   - 不再需要更多信息就立即收尾。规划轮数上限 {max_rounds} 轮，超限后系统
     会强制收尾（基于已有工具返回如实作答），不存在无限追问
6. 用户质疑/催促执行（"你真显示了？""到底跳了没？""别光说，带我去啊"）：
   - 真实性询问（质疑某操作是否真执行过/执行细节，如"屏幕上写了什么"）→
     看页面上下文 recent_executions=（跨轮执行记忆：**你自己**在本会话里执行过、
     系统验收过的动作记录，格式"· MM-DD HH:MM 动作行（行尾可能有「— …」实体摘要，
     见规则 6b）"；行首时间=**该次执行的发生
     时刻**（本机 +08:00 钟面，无需换算），行尾"（×N）"=同一动作在本会话内重复
     执行过 N 次（只列最近一次的时间）——时间与次数都是系统事实，可据实转述，
     不要自行推算或改写时间。它记的是你的执行，**不是访客的浏览痕迹/前端上报
     的页面状态**——不许拿"那是访客行为记录"当理由否认自己执行过）。
     记录里有对应执行 → 选 chat 直接收尾，据记录如实
     转述（含「」内实际内容/路径/开关状态以及发生时间），不规划任何工具、不重发；
     记录里没有对应执行 → 也选 chat 收尾，如实说"系统记录里没有这次执行"，
     不编造、不否认回执、不为了"补做"重新规划执行
   - 若质疑的是"某页面/内容是否存在"（"真有这个页面？""确定有这篇？"）→
     content_query 查证后据实作答（页面存在性是内容问题，不是执行真实性）
   - 再次要求（明确重发同款或升级指令——"别光说，带我去啊"= 要直接跳过去、
     "再显示一次刚才那句"）→ 属新请求：重新规划该动作技能并真实执行；
     navigate 填 mode=direct（免确认框直达）；不得零工具口头承诺
     "马上带你去/这就去"——上次正是口头说"已经在 X 页"才被质疑
6b. 指代取值优先于重查（20260920）：recent_executions 行尾的「— …」是那次执行取回的
   **实体摘要**（留言条目原文/分类文章数/文章候选标题等，系统按工具返回压成的事实）。
   用户指代"上文已经取回来过的东西"（"第二条写了什么""那个分类下面有几篇文章""刚才
   那个端口是多少"）：
   - 摘要里有该值（含序号对得上的条目）→ **选 chat 直接作答**，值照抄（数字、条目
     原文、「」内的字句不得改写或凑整），**不要为了取值把同一个工具再跑一遍**；
   - 摘要里没有该字段、或本会话没有对应执行行 → 才调用**同一个数据工具**取一次
     （禁止换 rag_search/search_notes 去绕：语义检索会命中同主题的另一篇）；
   - 指代对象在摘要里本身就**不唯一**（如"那个分类"，而摘要列了 5 个分类）→ 追问
     澄清是哪一项，不要默认挑第一个。
7. 输出严格按以下格式（JSON 双引号），不要任何其他文字：
SKILL: <技能名>
PARAMS: <JSON>
（多步链中间轮可另加一行：TODO: <步骤1> → <步骤2>，只描述本轮之后的
后续依赖步骤，单步/收尾轮不写）

用户消息：{user_msg}"""


# planner 菜单（可规划执行的查询工具清单）——20260913 起由 skills.py 白名单
# （_CALLABLE_QUERY_TOOLS_ORDER = 唯一事实来源）+ 工具注册表**生成**，不再手抄：
# 手写菜单曾只列 8 个工具，漏掉了站点信息/社交链接/分类/标签/置顶/天气这批数据
# 工具，于是"作者有哪些社交链接/备案号是多少"这类问题没有可点名的数据工具，
# planner 只能拿 rag_search/search_notes 去绕（检索索引只有文章正文，对站点元数据
# 零命中）→ 绕一圈如实答"站内没有"，而数据一直在 /social、/user（20260913 trace 实证）。
# 生成式菜单的契约：白名单里每个工具必在菜单中出现（test_skills 锁），新增工具
# 只需改 skills.py 一处。参数签名从 tool.args 派生。
# 动作工具不在白名单 → 结构性进不了菜单：planner 无法经 calls 通道越权动作，
# 只能由技能模板展开（skills.py 已论证）。
_TOOL_MENU_LINES: dict[str, str] = {  # 中文说明（缺省回退注册表 docstring）
    "search_notes": "按关键词搜文章（标题+内容），返回候选列表（含 id/标题/描述/封面）",
    "rag_search": "语义相关度检索（BM25），返回行式候选（type/id/score/标题/命中节，"
                  "用于定位，不给全文）",
    "get_article_detail": "读指定文档（doc_type=note|talk|board|announcement；超长文章"
                          "只带回部分小节时用 section 补读被略去的某一节；"
                          "article_id 只能取上一轮工具返回中的真实 id，或直接写引用 "
                          "$<工具名>[<序号>].<字段>，见规则 3b）",
    "list_notes": "分页列文章",
    "get_weather": "查天气（location：城市名，缺省北京）",
    "list_guestbook": "无参直取：留言板（河灯集）列表",
    "list_talks": "无参直取：说说（动态/碎语）列表",
    "get_announcements": "无参直取：博客公告列表",
    "get_current_time": "无参直取：当前日期时间",
    "get_blog_info": "无参直取：博客基本信息（作者/头像/签名/ICP备案号）",
    "get_social_links": "无参直取：社交链接（QQ/GitHub/BILIBILI/邮箱）",
    "get_site_map": "无参直取：博客功能结构图（有哪些页面/板块）",
    "get_top_notes": "无参直取：置顶文章列表",
    "list_categories": "无参直取：全部分类（名称/颜色/图标/文章数量）",
    "list_tags": "无参直取：全部一级标签",
}


def _tools_desc() -> str:
    """planner 菜单：白名单顺序 × 注册表（工具名 + 派生参数签名 + 中文说明）。

    白名单里有、注册表里没有的工具（配置错误）跳过并告警——它进不了 execute
    （_TOOL_MAP 查不到 → __ERROR__ 帧），列进菜单只会诱导 planner 点它。
    """
    lines = []
    for name in _CALLABLE_QUERY_TOOLS_ORDER:
        tool = _TOOL_MAP.get(name)
        if tool is None:
            logger.warning("[planner] 白名单工具 %s 不在注册表，菜单已剔除", name)
            continue
        args = ", ".join((getattr(tool, "args", None) or {}).keys())
        desc = _TOOL_MENU_LINES.get(name) or (tool.description or "").strip().replace("\n", " ")
        lines.append(f"- {name}({args})：{desc}")
    return "\n".join(lines)


_QUERY_TOOLS_DESC = _tools_desc()


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
    容错原则：所有"LLM 输出 → 程序消费"的边界都要能优雅降级——LLM 不是
    JSON 解析器，输出格式漂移是常态（解析失败 → 按 chat 兜底，宁可少干活）。
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
# 写站点内容的完成式声称（20260920，配 authz 的"人在回路确认"闸）：**只在错误帧
# 场景用**（gate 5a）——那一刻本轮必有 __ERROR__ 帧，而未获确认的写操作正是以错误帧
# 形态落地的，所以"未经确认 + 回复说已经发布"必须能被拦住。刻意不并进
# _EXECUTION_CLAIM_RE：那条还会用在零工具轮的宽查上，把 发布/提交 放进去会撞上
# "你已经提交过河灯啦" 这类转述用户过往动作的句子。
# 三支的取舍（都要求**动词后带完成标记**，这一条是误报的主闸）：
#   ① 带施事前缀（帮你/给你/为你/替你）——"已经帮你发布好啦"这种口吻隔着"帮你"两个字，
#      只写 `已(?:经)?发布` 会漏；前缀也把"你已经提交过河灯啦"这类**转述用户过往动作**
#      的句子挡在外面（那句没有施事前缀）。
#   ② 裸式 `已经发布/已经发表`——只认这两个动词；放开 `已(?:经)?提交` 就会撞上①里那句
#      被挡住的转述。
#   ③ `成功…` 自带完成语义。
# 完成标记（了/啦/好/完成/成功/完毕）距动词 ≤4 字：把"我能帮你把留言发出去吗"这类
# **提议/征询**与"已经帮你把留言发出去了"这类**声称**分开——5a 的误伤会吞掉整轮叙述，
# 而提问是未获确认时最正确的收尾（见 test_authz.py ⑨b 的两条端到端用例）。
_WRITE_CONTENT_CLAIM_RE = re.compile(
    r"(?:"
    r"(?:帮你|给你|为你|替你|帮主人)(?:把)?[^\n。！？!?；;，,]{0,12}?"
    r"(?:发布|发表|投稿|提交|上传|发出|发送)"
    r"|(?:已经?)(?:发布|发表)"                    # 裸式只认 发布/发表（见上）
    r"|成功(?:发布|发表|投稿|提交|上传|发出|发送)"
    r")"
    r"[^\n。！？!?；;，,]{0,4}?(?:了|啦|好|完成|成功|完毕)"   # 完成标记：区分声称与提议
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
# 工具名名单从注册表派生（20260913）：手写名单是漏项来源（15:51 事故里旧名单只
# 认 7 个工具名，新数据工具（get_social_links 等）不在内）；长名在前避免前缀冲突。
_TOOL_NAMES_ALT = "|".join(re.escape(n) for n in sorted(_TOOL_MAP, key=len, reverse=True))
# 裸名字分支保留旧 7 名（不随注册表扩展）：383 条真实 trace 回归显示，裸名字是
# 最松的一支，扩展后新增 4 例误伤（元讨论讲 function call 协议、转述留言板里那句
# "执行调用 navigate_to"、复述文章正文中的工具名）——这些语境没有"第一人称+动词"
# 约束，误伤成本高。带动词/第一人称的分支才用全量注册表名。
_LEGACY_TOOL_NAMES = ("get_current_time", "rag_search", "list_guestbook", "list_talks",
                      "get_announcements", "get_article_detail", "search_notes")
_LEGACY_TOOL_NAMES_ALT = "|".join(_LEGACY_TOOL_NAMES)
_CALLED_TOOL_CLAIM_RE = re.compile(
    r"调(?:用|过)(?:过)?(?:了)?\s*(?:工具|" + _TOOL_NAMES_ALT + r")"
    r"|调用了?(?:这个|那个|这些|两个|几个|三个)?工具"
    r"|(?:" + _LEGACY_TOOL_NAMES_ALT + r")"
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
    r"(?:我|咱|人家|本喵)(?:刚|刚才|刚刚|这轮|这一轮|之前|确实|真的|又|就|已经?|都|把)?(?:用|通过|拿|调)(?:了|过)?\s*(?:" + _TOOL_NAMES_ALT + r"|工具)"
    r"|(?:我|咱|人家|本喵)(?:刚|刚才|刚刚|这轮|这一轮|之前|确实|真的|又)?调(?:用|过)(?:过)?(?:了)?\s*工具"
    r"|(?:我|咱|人家|本喵)刚(?:刚|才)?(?:用|通过)\s*(?:" + _TOOL_NAMES_ALT + r")(?:查|搜|调|读|看|翻|拿|执行)"
)
# 匹配点**前**的否定/使役标记（20260920 误伤修复，见 _chat_tool_claim）。
_NEG_BEFORE_RE = re.compile(r"不是|并非|没有|不用|别|让|请|叫|要是|如果")
# 工具声称的完成态标记：比 _STATE_DONE_RE 多认"过"——"我用 X 查过时间"是完成式声称
# （真实用例），而 _STATE_DONE_RE 是给状态动作判据调的，那边裸"过"会撞"通过/经过"，不能收。
_CLAIM_DONE_RE = re.compile(r"了|过|已经|刚刚|方才|啦|咯|喽|成功|完成|搞定")
# ── gate 洞①：零工具轮的"操作完成"声称（20260919）────────────────────────────
# 事故实证（真实 trace 20260907 12:47:53）：用户只说"嗯"，planner 判 chat（零工具），
# narrator 却回"那泠月喵就帮你把夜间模式关掉，回到明亮的日间页面啦！"——页面其实没变
# （零帧 = 本轮什么都没发生），gate 判 PASS（_EXECUTION_CLAIM_RE 词表刻意不含
# 开启/关闭/切换，为的是不误伤幂等轮的合法状态陈述"樱花已经开着"），访客被误导。
# 判据（只在零帧轮启用，见 _claim_issue）：**施事前缀 + 及物状态动作动词**
# **+ 同句完成态**——"帮你把 X 关掉…啦"/"已经帮你打开了"/"已经切到夜间模式了"。
# 与陈述态的区分靠四点：
#   ① 陈述态用"着/是…状态"（开着、是开启状态），不在动词表里；
#   ② 提议/能力/假设（能/可以/会/要不要/如果/随时/吧/？）走豁免表；
#   ③ 否定如实（没有/不用/别）走豁免表；
#   ④ **完成态标记**（已经/刚刚/啦/了…）——**句子级**，不是子句级：事故句的完成
#      标记落在同句后半"回到明亮的日间页面**啦**"。这一条是 20260919 真实 trace
#      全量复扫抓出来的误伤补丁：20260907 22:01「小猫咪你都有哪些工具」的回答是
#      **能力清单**——"帮你开启或关闭樱花""给你可点击的链接跳转过去"——旧判据把
#      前者当成了操作声称（清单体的动词没有完成态，也没有"已经"）。零帧轮误伤
#      代价是整轮回复被 fallback 吞掉，故按能力罗列/条件句收窄（宁漏勿误伤）。
#   ⑤ **无施事标记时只认自带施事语义的动词**（20260920 加，见正则②③支）：开合类
#      动词与"状态陈述"同形——"樱花特效已经开启啦"就是**幂等轮的正确答案**（planner
#      零调用 + 状态本就匹配 ⇒ 这正是该说的话）。要抓动作声称得有显式施事标记：
#      "帮你/给你"（①支）或把字结构"已经把…打开了"（③支）。
#   ⑥ **完成态不早于匹配起点**（20260920 加，见 _clause_hits 的 need_done）：
#      标记不可能出现在动作之前。两处实证误伤（exec_memory_none_honest 的高频措辞，
#      探针复现）——"**为了**确认清楚，我现在重新帮你把…显示一下，稍等哦～"（"了"
#      落在前一个子句的「为了」里）、"抱歉让你白等**啦**～我现在就帮你把…显示到
#      屏幕上"（"啦"挂在道歉语上，不是在说显示动作已完成）。
_STATE_ACTION_CLAIM_RE = re.compile(
    r"(?:我|咱|人家|本喵|泠月喵|系统|喵)?(?:已经?|刚刚|方才)?"
    r"(?:帮你|给你|为你|替你|帮主人|帮你把|给你把)"
    r"[^\n。！？!?；;，,]{0,16}?"
    r"(?:打开|开启|开好|关掉|关闭|关上|切换|切到|切成|切回来|调到|改成|换成|显示|上屏|跳转|跳过去"
    # 20260920：写站点内容的动词（发布/发表/投稿/提交）**只进这一支**——它要求
    # 施事前缀（帮你/给你/为你/替你），所以"你已经提交过河灯啦"这类**转述用户自己
    # 的过往动作**不会被误判；而把动词放进②支就会撞上它。写工具的"人在回路"确认
    # 闸（agent/authz.py）落地后，这条同时封住"零工具轮声称帮你发布了"的编造。
    r"|发布|发表|投稿|提交)"

    # ② 无施事标记的完成式：只认清**自带施事语义**的动词（切换/显示/跳转…）。
    #    开合类（打开/开启/关掉/关闭）**不进这一支**——汉语里"樱花特效已经开启啦"
    #    是**状态陈述**（幂等轮 planner 零调用时的正确答案），与"我把它打开了"同形。
    #    20260920 实证误伤：golden eff_state_consistent（"樱花特效是不是已经开了"，
    #    context current_effects=sakura）narrator 答"是的，樱花特效已经开启啦～"，
    #    被这一支判成操作声称 → 整轮 fallback（"我刚才说『已经帮你打开了』是不对的"
    #    ——为一句它没说过的话道歉，还答非所问）。三跑两放一拦，同一个判据在温度
    #    0.7 下随机误伤。**修好 channel 之前这个误伤不可见**（fallback 从不生效）。
    r"|(?:已经?|刚刚|方才)[^\n。！？!?；;，,]{0,10}?"
    r"(?:切换|切到|切成|切回来|调到|改成|换成|显示|上屏|跳转过去|跳转)"
    r"(?:了|啦|好了|成功)"
    # ③ 把字结构 = 显式施事标记（宾语被"把"提前 ⇒ 动作性明确，不是状态陈述）：
    #    "已经把夜间模式打开了"照抓；"樱花特效已经开启啦"没有"把"，落到②之外 → 放。
    r"|(?:已经?|刚刚|方才)[^\n。！？!?；;，,]{0,10}?(?:把|将)[^\n。！？!?；;，,]{0,10}?"
    r"(?:打开|开启|开好|关掉|关闭|关上|切换|切到|切成|切回来|调到|改成|换成|显示|上屏|跳转过去)"
    r"(?:了|啦|好了|成功)"
)
_STATE_ACTION_EXEMPT_RE = re.compile(
    r"没|没有|未|不曾|从未|无法|不能|不会|不用|不需要|无需|别|并不是|不是"
    r"|可以|能够|会|能|如果|若是|要是|若|要不要|需要的话|建议|随时|待会|等下|马上|这就|接下来|准备|打算|想要|想"
    r"|你说|你问|你提到|引用|原话|么|吗|呢|吧|[?？]"
)
# 句子切分（完成态标记的作用域）与"已完成"标记本身（见 _state_action_claim）。
# 只认完成态虚词与时间副词：裸"已"会撞"而已"、裸"好"会撞"好呀"，故不收；
# 裸"了"要排除功能词「为了/除了/罢了/算了」里的那个（不是完成态）——20260920 实证：
# exec_memory_none_honest 的高频措辞「**为了**确认清楚，我现在重新帮你把…显示一下，
# 稍等哦～」被「为了」的"了"当成了完成态 → 洞①误伤（探针 40 跑 1 中，属真实复现）。
_SENT_RE = re.compile(r"[。！？!?\n]+")
_STATE_DONE_RE = re.compile(r"已经|刚刚|方才|啦|咯|喽|好了|(?<![为除罢算])了|成功|完成|搞定")
# ── gate 洞②：站内检索声称 vs 本轮帧族（20260919）──────────────────────────
# 事故形态：回复说"我检索了一圈 / 把站内翻了一遍 / 用 rag_search 搜了一遍"，而本轮
# 根本没跑任何内容类工具（零帧，或只跑了导航/特效这类动作工具）。旧判据两处缺口：
#   ① _CHAT_SCAN_CLAIM_RE 只在 chat 零工具轮跑，且要求人称在空间词**之前**——
#      真实 trace 20260906 23:49「站内我查了一圈，没有找到专门讨论…的文章或说说」
#      （零工具）因此漏网；
#   ② 有帧轮（混合轮）只做具名工具核对（5c），泛指"检索了一圈"不点名工具 → 漏网。
# 现判据 = 站内内容域检索声称 + 本轮内容类工具一个都没跑（_CONTENT_TOOLS）。豁免
# 与 5c 同源：否定/提议/假设、引述（调用方先去引号）、跨轮回执支撑的追述。
_SITE_SEARCH_CLAIM_RE = re.compile(
    r"(?:站内|全站|博客|网站|站点|文章库|你写的|博主写的|所有文章|全部文章|全部说说)"
    r"[^\n。！？!?；;，,]{0,24}?"
    r"(?:扫|查|翻|搜|检索|翻找|查找)(?:了|过)?(?:个)?(?:一圈|一遍|个遍|好几圈|个底朝天|遍)"
    r"|(?:搜|检索|翻|查)(?:了|过)?(?:个)?(?:一圈|一遍|个遍|好几圈)(?:站内|博客|文章|说说|留言|全站)?"
    r"|用\s*`?\w{3,}`?\s*(?:搜|查|检索|翻)(?:了|过)?(?:一圈|一遍|个遍|一遍)"
    r"|(?:两|双)(?:边|侧|个)(?:板块|数据源)?(?:都|也)?(?:真的)?(?:翻|查|看|搜)(?:了|过|完)"
)
_SEARCH_CLAIM_EXEMPT_RE = re.compile(
    r"没|没有|未|不曾|从未|无法|不能|不会|不用|不需要|无需|别|并不是|不是"
    r"|可以|能够|会|能|如果|若是|要是|若|要不要|需要的话|建议|随时|待会|等下|接下来|准备|打算|想要|想"
    r"|网上|网络|互联网|通用|常识|知识库|训练|资料里"
    r"|你说|你问|你提到|引用|原话|吗|呢|[?？]"
)
# 本轮"内容类"工具（跑过任何一个 ⇒ 检索/读取声称有据，5d 不判）。名字取自
# tools/base.py 注册表（test_skills 有断言锁定全部 ∈ _TOOL_MAP，防改名漂移）。
_CONTENT_TOOLS = frozenset({
    "search_notes", "rag_search", "list_notes", "get_top_notes", "list_talks",
    "list_guestbook", "list_categories", "list_tags", "get_announcements",
    "get_article_detail", "get_site_map", "get_blog_info",
})
# 命令前缀文本：回复正文出现系统命令帧前缀 = 模型在"假装发命令"（旧事故：正文
# 输出 AUTO_NAVIGATE:/NAVIGATE:/EFFECT:/DARKMODE: 文本既不会执行、还误导用户
# 以为已执行）。任何轮次命中一律兜底——叙述纪律已禁止，命中即确凿违规。
# 20260920 收窄（元讨论豁免）：**提及**不是发命令（见 _cmd_prefix_directive）。
_CMD_PREFIX_RE = re.compile(r"(?:AUTO_NAVIGATE|NAVIGATE|EFFECT|DARKMODE)\s*[:：]")
# 机制/元讨论语境标记（同句出现 ⇒ 那句话在**讲命令机制**，不是在发命令）
_CMD_META_RE = re.compile(
    r"系统|命令|前缀|正则|协议|帧|机制|实现|代码|文档|校验|核对|拦截|拦下|剔除|过滤"
    r"|白名单|提示词|cleanAgentText")


def _cmd_prefix_directive(text: str) -> bool:
    """回复是否**指令式**地写了命令前缀（返回 True = 违规，走 fallback）。

    20260920 元讨论豁免：旧判据对全文裸搜 `_CMD_PREFIX_RE`，把"讲命令机制时举的例子"
    也判成发命令。现场（golden rag_arch_check，用户问"怎么防止假装调用工具"，模型
    答"……就算在正文里写 `NAVIGATE:/xxx` 也会被前端的 `cleanAgentText` 剔除……"）
    → 用户收到的是兜底道歉，而这条回复本身完全正确。同一根因在 9/20 全量里 2 例
    （rag_arch_check / followup_named_doc_reread，后者还被判 PASS——正断言恰好
    能被道歉文本命中，见 golden `forbid_fallback` 断言）。

    判定：出现处**必须同时**满足 ①落在引号或内联代码区内 ②所在句子含机制词，
    才算"提及"放行；任一不满足即仍判违规——两种需要继续拦的形态：裸写在正文里
    （"我这就打开 `EFFECT:x`"的裸形式）、代码区内但在讲**要做的事**而不是机制
    （"稍等～ `EFFECT:sakura:on`"）。副作用是这类字符串不再被前端当命令执行：
    前端 `execAgentCommands` 的正文兜底同步跳过引号/代码区（chat-core.js
    stripMentionSpans），两侧口径必须一致，否则放行的提及会在页面上真的生效。"""
    for m in _CMD_PREFIX_RE.finditer(text):
        i = m.start()
        if not (_inside_quote(text, i) or _inside_code_span(text, i)):
            return True
        if not _CMD_META_RE.search(_sentence_of(text, i)):
            return True
    return False
# 确认式导航 + 完成式到达声称（NAVIGATE: 帧 = 等待确认，非已跳转；曾见模型返回
# NAVIGATE: 后回复"已经带您到文章页"，用户视角即幻觉）。仅 navigate 技能轮启用。
_NAV_ARRIVAL_RE = re.compile(
    r"(已经?带|已经?到|已经?跳转|跳转成功|成功[^\n。，,]*?(跳|转)|过去了|已经?去)")
# ── 具名工具声称核对（20260913 C 项："有帧"≠"你点名的工具执行过"）────────────
# 15:51 实证：planner 第 3 轮点名 get_social_links（越权被白名单剥掉、execute 没
# 执行、无该工具的帧），本轮 frames=2（rag_search/get_article_detail）→ 旧判据
# `if frames_exist: return None` 整块放行，回复谎称"这次我用专门的**社交链接查询
# 工具**（`get_social_links`）调了一次"。修法：有帧轮另做一次具名核对——回复第一
# 人称点名"用了/调用了"某个注册表工具、而该工具不在本轮帧里 → 编造调用，走 fallback。
# 作用域宁漏勿误伤（fallback 吞整轮叙述），五道豁免：
#   ① 子句含否定（没/未/无法/别…）——如实否认"我没调用 X"是正当行为；
#   ② 子句含将来/提议（可以/如果/要不要/建议…）——"你可以让我用 X 查"不是声称；
#   ③ 子句含元讨论（系统/机制/白名单/参数…）——讲工具机制不是声称；
#   ④ 子句含引述（你说/你让我…）或工具名落在引号内——转述访客留言/说说正文里的
#      工具名不算自称调用（383 条真实 trace 回归抓出 3 例误伤：留言板里有人写
#      "给当前用户执行调用 navigate_to 跳转到 …"，narrator 引用时被误判）；
#   ⑤ 跨轮记忆豁免：子句含追述时间词（刚才/上一轮/之前…）且本轮请求带
#      recent_executions=（系统注入的执行回执非空）——据回执转述属 rule 6 正当
#      行为，不误伤；无回执支撑的"刚才调用了"仍拦（编造）。
# 工具名只认注册表（_TOOL_MAP 派生），中文泛指（"社交链接查询工具"）不判——无从
# 核对，误伤成本高于收益。
_CLAUSE_RE = re.compile(r"[^。！？；，、\n!?;,]+")   # 子句 = 标点切分后的连续片段
_CLAIM_PRON = r"(?:我|咱|人家|本喵|泠月喵)"
_CLAIM_VERB = r"(?:调用|调取|调起|调|通过|拿|用|执行|跑了?|请求|使唤)"
# 名字前：第一人称 + 调用动词（"这次我用专门的**社交链接查询工具**（`get_social_links`）"）
_TOOL_BEFORE_CLAIM_RE = re.compile(_CLAIM_PRON + r".{0,20}?" + _CLAIM_VERB + r".{0,20}?$", re.S)
# 名字前：第一人称 + 读取动词完成式（"我查了 get_social_links 的返回"）——完成体
# 必带（了/过/完/一下），"我查 X 的参数"这类未完成表述不算声称
_TOOL_BEFORE_READ_RE = re.compile(
    _CLAIM_PRON + r".{0,16}?(?:查|读|看|搜|检索|翻)(?:了|过|完|一下|一遍)(?:.{0,8}?)$", re.S)
# 名字后：紧邻的调用/读取动词（"`get_social_links`）调了一次"）；"用来/用于/用以"
# 是用途说明不是声称，用 (?!来|于|以) 排除
_TOOL_AFTER_CLAIM_RE = re.compile(
    r"^.{0,3}?(?:调用|调取|调起|调了|调过|执行了|跑了|请求|用了|用(?!来|于|以)|查了|读过|读了|看过|看了)", re.S)
_PHANTOM_EXEMPT_RE = re.compile(
    r"没|没有|未|不曾|从未|无法|不能|不会|不用|不需要|无需|别|并不是|不是"
    r"|可以|能够|会|能|如果|若是|要是|若|要不要|需要的话|建议|随时|马上|待会|接下来|准备|打算|想要|想|让我|帮你"
    r"|系统|机制|白名单|注册表|工具描述|参数|字段|接口|代码|文档|工具名|清单|这类|那种|比如|例如|举例"
    r"|你说|你说的|你问|你提到|你让我|引用|原话"
)
_PHANTOM_PRIOR_RE = re.compile(
    r"刚|之前|先前|上次|上一轮|上轮|前几轮|那次|早先|上回|前面|早前|记录|历史")
_TOOL_NAME_RE = re.compile(_TOOL_NAMES_ALT)


def _has_exec_memory(msgs: list) -> bool:
    """本轮请求是否带跨轮执行记忆（server.py 仅在 executions 非空时才注入
    recent_executions=，故"出现即非空"）。"""
    return any("recent_executions:" in str(getattr(m, "content", "")) for m in msgs)


# 引号区段（成对才算，避免英文撇号等单边字符误吞整段）：被引内容 = 转述访客留言/
# 说说正文，不算 narrator 自己的声称（383 条真实 trace 回归：留言板里"执行调用
# navigate_to"被转述时误伤 3 例）
_QUOTED_SPAN_RE = re.compile(r"“[^”]*”|「[^」]*」|『[^』]*』|\"[^\"]*\"")


def _quoted_spans(text: str) -> list[tuple[int, int]]:
    return [(m.start(), m.end()) for m in _QUOTED_SPAN_RE.finditer(text)]


def _strip_quoted_spans(text: str) -> str:
    """去掉引号内的内容——声称闸只判 narrator 自己说的话。"""
    return _QUOTED_SPAN_RE.sub("", text)


# 内联代码区（含 ``` 围栏；先配对短跨度即天然吃掉围栏内容，见 20260920 元讨论豁免）
_CODE_SPAN_RE = re.compile(r"`[^`]*`", re.S)


def _inside_code_span(text: str, pos: int) -> bool:
    """pos 处是否落在内联代码/围栏区内（举例说明 ≠ 发命令）。"""
    return any(m.start() <= pos < m.end() for m in _CODE_SPAN_RE.finditer(text))


def _inside_quote(text: str, pos: int) -> bool:
    """pos 处是否落在引号区内（转述他人内容不算自称调用）。"""
    return any(s <= pos < e for s, e in _quoted_spans(text))


_SENT_BREAK = "。！？；\n!?;"


def _sentence_of(text: str, pos: int) -> str:
    """pos 所在的句子（按句末标点/分号/换行切，逗号留在句内）。

    判据作用域单位取**句子级**（不是子句级）：元讨论常把"命令帧有这几种："
    与被举的例子隔一个逗号放在同一句里，而"好的～我这就打开"这种施事句整句
    不含机制词——子句级会漏掉前者、句子级不会放大后者。"""
    start = max((text.rfind(c, 0, pos) for c in _SENT_BREAK), default=-1) + 1
    end = min((i for i in (text.find(c, pos) for c in _SENT_BREAK) if i >= 0),
              default=len(text))
    return text[start:end]


def _tool_claim_window(full: str, start: int, end: int, name: str) -> bool:
    """工具名两侧的声称窗口：名字前有"第一人称+调用动词"或名字后紧接调用动词。
    引号内的出现（转述访客留言/说说正文）直接跳过——引号状态按全文判定（引号常
    与被引内容被逗号切开，只看子句会漏）。"""
    for m in re.finditer(re.escape(name), full[start:end]):
        p = start + m.start()
        if _inside_quote(full, p):
            continue
        if (_TOOL_BEFORE_CLAIM_RE.search(full[start:p])
                or _TOOL_BEFORE_READ_RE.search(full[start:p])
                or _TOOL_AFTER_CLAIM_RE.search(full[p + len(name):end])):
            return True
    return False


def _phantom_tool_claim(reply: str, executed: set[str], exec_memory: bool) -> str | None:
    """有帧轮的具名工具声称核对：回复点名"我调用过"的注册表工具不在本轮帧里 →
    返回该工具名；无此情况 → None。判据与豁免见上方注释块。"""
    if not executed:
        return None
    text = reply.replace("`", "").replace("*", "")   # markdown 装饰不参与判词
    for c in _CLAUSE_RE.finditer(text):
        clause, start, end = c.group(0), c.start(), c.end()
        names = [n for n in dict.fromkeys(_TOOL_NAME_RE.findall(clause)) if n not in executed]
        if not names or _PHANTOM_EXEMPT_RE.search(clause):
            continue
        if exec_memory and _PHANTOM_PRIOR_RE.search(clause):
            continue
        for name in names:
            if _tool_claim_window(text, start, end, name):
                return name
    return None


def _clause_hits(text: str, rx, exempt, need_done: bool = False, veto=None) -> bool:
    """子句级判定：任一无豁免词的子句命中 rx → True。

    子句切分沿用 _CLAUSE_RE（标点切分）——豁免必须**同子句内**才算数：
    "那泠月喵就帮你把夜间模式关掉，要是之后想换回来随时说" 里前句是声称、
    后句的"要是/随时"不该豁免前句。

    need_done=True：完成态标记必须落在**中间位置之前**——即同句内、且**不早于匹配
    起点**（见 _STATE_DONE_RE 注释）——完成标记不可能出现在动作之前。起点取匹配起点
    而非子句起点：实证误伤 "抱歉让你白等**啦**～我现在就帮你把「欢迎回来」显示到
    屏幕上"——"啦"挂在道歉语上（"让你白等啦"），不是在声称显示动作已完成。
    但作用域不能收到子句级：事故句的完成标记落在同句**后半**（"…就帮你把夜间模式
    关掉，回到明亮的日间页面**啦**"），那仍是"已关掉"的完成态。

    veto(clause)：额外的逐子句放行判据（返回 True = 该子句不算声称），在 exempt 之后、
    正则之前生效——给"回执在场 + 追述时间词"这类**由调用方状态决定**的豁免用
    （见 `_state_action_claim`）。"""
    for s in _SENT_RE.split(text):
        for c in _CLAUSE_RE.finditer(s):
            clause = c.group(0)
            if exempt.search(clause):
                continue
            if veto and veto(clause):
                continue
            m = rx.search(clause)
            if not m:
                continue
            if need_done and not _STATE_DONE_RE.search(s, c.start() + m.start()):
                continue
            return True
    return False


def _prior_time_veto(exec_memory: bool):
    """回执在场时，"追述时间词"子句 = 据实转述（rule 6），不判声称。"""
    if not exec_memory:
        return None
    return _PHANTOM_PRIOR_RE.search


def _state_action_claim(text: str, exec_memory: bool = False) -> bool:
    """零工具轮的"操作完成"声称（gate 洞①）：施事前缀 + 及物状态动作动词 + 完成态。

    exec_memory=True（本轮带跨轮执行回执）且子句含**追述时间词** → 属 rule 6 的据实
    转述，不判（20260921 补，与洞② 的 `_site_search_claim` 同款规则——两个洞共用同一族
    误伤：回执在场时"刚才/之前"指向的是**已记录的执行**，不是本轮的空手套）。

    实证（`/tmp/rescan_state_claim.py` 全库复扫 498 条真实 trace，uid≠0）：
      现行判据命中 18 轮；其中会被本豁免放行的 **2 轮**，两轮都发生在 2026-09-03
      （execution_log 20260904 才上线 ⇒ 那两轮 `_has_exec_memory` 本就为 False，
      豁免**不会**生效）⇒ 在"回执在场"这个真实触发条件下，历史放行数 = 0。
      修的是 golden `exec_memory_display_quote` 实测的另一种误伤：访客问"你刚才说显示
      上去了，真的假的？"，narrator 据回执答"刚帮你显示上去了"，被判成零帧编造，
      整轮换成兜底道歉——而那段兜底还反过来说"这一轮系统没有任何工具执行…我刚才说
      已经帮你打开了是不对的"，**与执行回执直接矛盾**（同例单跑 3/3 PASS，属低概率触发）。
    """
    return _clause_hits(text, _STATE_ACTION_CLAIM_RE, _STATE_ACTION_EXEMPT_RE,
                        need_done=True, veto=_prior_time_veto(exec_memory))


def _chat_tool_claim(text: str) -> bool:
    """零帧 chat 轮的第一人称工具调用声称（_CHAT_TOOL_CLAIM_RE）。

    20260920 收窄（真实 trace 00:26:35 误伤）：`我调工具` 命中的是**否定+使役**句——
    "那次跳转**不是你让我调工具**做的，更像是导航正则快道直接接管了…"——narrator 说的
    正是"我没调"，却被判成调用声称，整轮 fallback（用户拿截图来质问，narrator 的诚实
    认错被吞掉，访客拿到"被主人抓包啦"，为一件它根本没做的事道歉）。两条与洞①②同源
    的纪律（零帧误伤代价 = 整轮回复被吞，宁漏勿误伤）：
      ① 匹配点前 6 字内有否定/使役标记（不是/并非/没有/让/请/叫/要是/如果…）→ 跳过；
      ② **同句完成态**要求（_STATE_DONE_RE）——裸"我调工具"是描述/假设性片段，不是
         "已做过"的声称；真声称（"我用了 X 工具查的""这次我调用工具查了一遍"）自带完成态。
    全库复扫（453 条带事件 trace，零帧轮 127 条）：该判据真声称命中 0、误伤 1（即上述
    事故句）——收窄后误伤清零，且不影响洞①②自己的命中性。
    """
    for s in _SENT_RE.split(text):
        if not _CLAIM_DONE_RE.search(s):
            continue
        for c in _CLAUSE_RE.finditer(s):
            clause = c.group(0)
            for m in _CHAT_TOOL_CLAIM_RE.finditer(clause):
                if _NEG_BEFORE_RE.search(clause[max(0, m.start() - 6):m.start()]):
                    continue
                return True
    return False


# ── gate 洞③：有帧轮里谎称"本轮没有执行任何工具"（20260920）──────────────────
# 与 5c/5d 方向相反的**假阴性**声称。事故实证（真实 trace 20260920 00:56:23）：本轮
# search_notes 真执行（返回空 `[]`、checker PASS、frames=1），叙述却写"**本轮没有执行
# 任何检索工具**（回执为空）"——访客的肯定应答（"要"，承接上一轮"要不要我正经跑一次
# 检索"）被吞掉，又被反问"要不要我查一遍" ⇒ 确认死循环（用户看着像"我说要它也没查"）。
# 根因在提示词侧："空结果"与"没执行"同形（帧渲染 `返回: []`、回执模板"为空 = 本轮没有
# 已验收的执行"、纪律 3 的"（本轮尚无工具执行）"现成句式），模型套错了句式——渲染侧
# 已同步标注"（已执行，结果为空）"（context.py）并在纪律 3 加了反向说明，本判据是兜底。
# 判据只在**有已验证回执**时启用（receipts 非空）：零帧轮该表述是**真话**（全库 3 条
# 真实 trace 实证）、帧全被 BLOCK（__ERROR__/空文本）时"没有执行"也算属实，都不能拦。
_NO_EXEC_CLAIM_RE = re.compile(
    r"(?:本轮|这轮|这一轮|本次|这次|刚才|刚刚)[^。！？\n]{0,16}(?:没有|没|未)(?:有)?"
    r"(?:执行|调用|跑|做|触发)[^。！？\n]{0,12}(?:任何|一个)[^。！？\n]{0,4}工具"
    r"|(?:本轮|这轮|这一轮)[^。！？\n]{0,20}回执(?:是|为)?空的?"
)
# 洞③专用豁免（**不能复用 _STATE_ACTION_EXEMPT_RE**：那张表收"没/没有/未"，而本判据
# 的命中本身就含否定词，复用等于全豁免）。只豁免条件/假设/疑问框架——"要是本轮没有
# 执行任何工具，我就…"是假设不是声称。
_NO_EXEC_EXEMPT_RE = re.compile(
    r"要是|如果|假如|假设|除非|若|为什么|是不是|难道|吗|呢|[?？]|引用|原话")


def _false_negative_claim(reply: str, receipts_exist: bool) -> bool:
    """有帧轮的"本轮什么都没执行"假阴性声称（见 _NO_EXEC_CLAIM_RE）。

    豁免：引号内是转述（_strip_quoted_spans，调用方已做）；否定+条件句（"要是本轮没有
    执行任何工具…"）走 _CLAUSE_RE 同子句豁免表。只认"工具/回执"笼统表述——"本轮没有
    执行任何跳转操作"这类**具体某类动作**的如实说明不在此列（只跑检索时它是真话）。
    """
    if not receipts_exist:
        return False
    return _clause_hits(reply, _NO_EXEC_CLAIM_RE, _NO_EXEC_EXEMPT_RE)


# ── gate 1b：逐字复读上一轮回复（20260920）──────────────────────────────────
# 纪律 11（"绝不把历史里自己的回复原文再输出一遍"）只是**软约束**，压不住：
# 20260920 实证 narrator（温度 0.7）在"本轮用户消息模糊 + 上下文里摆着上轮一份
# 完整答案"时会把上轮回复整段抄出来——11:15:44 那条与 11 小时前 00:23:52 那条
# **逐字节相同**（781 字，difflib diff 为空）。0.7 采样自由生成撞出 781 字全同的
# 概率可忽略 ⇒ 它是从注入历史（最近 20 条纯历史，那条回复正好落在窗口里）里抄的。
# 抄写有机会是因为被 gate 否定的叙述仍进了历史（fallback_text channel 缺失，见
# _fallback_result）；修好 channel 后本判据是第二道闸：**不许把复读当成回答**。
# 判据（确定性、纯字面）：本轮回复与上一轮 assistant 回复的最长逐字连续片段
# ≥ max(200 字, 60% × 本轮长度) ⇒ 复读。门槛刻意高（宁漏勿误伤）：**只抓"整段
# 照抄"**。门槛由全量 trace 回放定（426 对相邻轮，见 eval 之外的一次性脚本能力）：
# floor=80 命中 4 对，其中两对是 90 字级的"两次都回答我不会做饭/烤蛋糕"——同义
# 寒暄撞同一句模板，应答本身合理，拦下来才是误伤；一对是用户点名重做（走
# _REDO_REQUEST_RE 放行）；抬到 200 后只剩 1 对真复读（09-05T19:10:07，489 字
# 逐字照抄）。合理复用（引用同一段工具返回、复述要点的两三句）远达不到门槛。
# 代价：≤200 字的回复不判复读——短回复的整段重合几乎都是模板复用，宁漏勿误伤。
# 与 _repeat_ask_note（server.py：用户**原句重发**时注入提示）分工不同——那个管
# 输入端（让模型别复读），本判据管输出端（真复读了就拦），触发条件也无关。
_REPEAT_MIN_RUN = 200
_REPEAT_COVER = 0.6

# 用户点名要求重做/重发 → 本轮高重合是**被要求的**，不得判复读（20260916 09:25:34
# 实证：用户说"flowchart 换回 graph 试试"，回复把 1400 字 mermaid 图原样重画、
# 只换了栅栏语言与开头一句——那是正确行为，拦下来等于把用户点名要的东西吞掉）。
# 只用于**放行**（宁漏勿误伤）：用户没这么说而复读 = 真复读。
_REDO_REQUEST_RE = re.compile(
    r"再(?:画|说|写|发|来|贴|试)|重新|重来|重发|重画|换个|换成|换回|换用|换一版|"
    r"改成|改一下|改一版|另一(?:个|种|版)|同一(?:个|张)图")


def _prev_ai_reply(msgs: list) -> str:
    """上一轮 assistant 回复原文：当前用户消息之前的最近一条 AI 消息。

    注入历史形状 = [System 上下文 Human] + 历史(Human/AI 交替) + 当前 Human +
    本轮工具帧 + 本轮 AI 回复——从末尾往前先定位"当前用户消息"（最后一条非
    `[System:` 的 HumanMessage），再往前找最近的 AIMessage：它就是"用户这句话
    在回答的那一轮"，也才是"复读"该对比的对象（更早的轮次不比对，避免长会话里
    翻旧账误伤）。无则返回空串（首轮无对比对象，判据自动放行）。
    """
    cur = None
    for i in range(len(msgs) - 1, -1, -1):
        m = msgs[i]
        if isinstance(m, HumanMessage) and not (_msg_text(m) or "").lstrip().startswith("[System:"):
            cur = i
            break
    if cur is None:
        return ""
    for m in reversed(msgs[:cur]):
        if isinstance(m, AIMessage):
            return (_msg_text(m) or "").strip()
    return ""


def _repeat_of_prev_reply(reply: str, prev: str, user_msg: str = "") -> bool:
    """本轮回复是否逐字复读上一轮回复（门槛见 _REPEAT_MIN_RUN 注释）。

    实现是最朴素的滑窗：取较短文本为窗口源、较长者为被查串，窗口长度 = 门槛，
    首个命中即判真（只需"是否达到门槛"，不求真正的最长值）。回复量级千字、
    str 查找是 C 级，无需后缀自动机。`user_msg` 带重做语（_REDO_REQUEST_RE）时
    直接放行——那是照办，不是复读。
    """
    if not reply or not prev:
        return False
    if user_msg and _REDO_REQUEST_RE.search(user_msg):
        return False
    short, long_ = (reply, prev) if len(reply) <= len(prev) else (prev, reply)
    thr = max(_REPEAT_MIN_RUN, int(len(reply) * _REPEAT_COVER))
    if len(short) < thr:
        return False
    return any(short[i:i + thr] in long_ for i in range(len(short) - thr + 1))


def _site_search_claim(text: str, exec_memory: bool) -> bool:
    """站内检索声称（gate 洞②）：站内内容域检索完成式表述。

    _CHAT_SCAN_CLAIM_RE 一并纳入（它的词表是 20260905 事故现场调过的，
    只是词序漏了"站内我查了一圈"形态）。exec_memory=True（本轮带跨轮回执）
    且子句含追述时间词 → 属 rule 6 的据实转述，不判。同 _state_action_claim
    一样要求**同句完成态**（整段话与洞①共用一条判据纪律：完成态才算声称）。"""
    text = "".join(s + "。" for s in _SENT_RE.split(text) if _STATE_DONE_RE.search(s))
    for c in _CLAUSE_RE.finditer(text):
        clause = c.group(0)
        if not (_SITE_SEARCH_CLAIM_RE.search(clause) or _CHAT_SCAN_CLAIM_RE.search(clause)):
            continue
        if _SEARCH_CLAIM_EXEMPT_RE.search(clause):
            continue
        if exec_memory and _PHANTOM_PRIOR_RE.search(clause):
            continue
        return True
    return False


# NOTE 零工具（页面不存在/已下线）轮的如实措辞核验词表（与 instantiate_plan 的
# note 文本配套，见 gate_node）。
_HONEST_DOWN = ("下线", "下架", "无法访问", "没有了")
_HONEST_GONE = ("没有", "不存在", "找不到", "无法识别", "没有找到")


def _claim_issue(reply: str, skill: str, plan: dict, frames_exist: bool,
                 exec_memory: bool = False) -> tuple[str, str] | None:
    """声称闸判定（gate 确定性兜底，20260902 事故族）：回复含声称但轨迹无工具
    支撑 → 返回 (issue, 人设内 fallback 文本)；有据/无声称 → None。

    作用域（20260903 收窄后的设计 + 20260919 两洞 + 20260920 洞③）：
      - 任何轮：命令前缀文本（_cmd_prefix_directive——引号/内联代码区 + 同句机制词
        = 元讨论里的提及，放行；见该函数注释与 golden `forbid_fallback`）
      - 零工具轮（不分技能）：操作完成声称（_STATE_ACTION_CLAIM_RE，洞①）与
        站内检索声称（_site_search_claim，洞②）——零帧 = 本轮什么都没发生，
        这两族声称必为编造。**两族共用同一条回执豁免**（20260921 补齐）：本轮带跨轮
        执行回执（executions 注入）且子句含追述时间词 → 说的是**已记录的那次执行**，
        属 rule 6 据实转述；此前只有洞② 接了 exec_memory，洞① 漏了，导致
        "刚才已经帮你显示上去了"这类**引回执**的回合被整轮换成兜底道歉
      - chat 零工具轮：另查第一人称工具调用声称（_CHAT_TOOL_CLAIM_RE）——
        高精确模式；"重读/查过"读取声称不在此拦（chat 轮多为口语，误伤成本高）
      - content_query 零工具轮（异常路径：计划本应有调用清单却留空收尾）：
        三族全查（读取/执行/调用声称）——该场景"本该查证"，声称误伤成本低
      - 有帧轮：读取/调用声称天然有据，不做文本对照；只兜 err 帧 + 完成式
        声称、NAVIGATE: 确认帧 + 到达声称、具名工具声称（5c）、**站内检索声称
        与内容类帧族不符**（5d，洞②的混合轮形态）——见 gate_node
    """
    if _cmd_prefix_directive(reply):
        return ("cmd_prefix", _FALLBACK_CMD_PREFIX)
    if frames_exist:
        return None  # 帧存在：声称有据（err 帧/确认帧/具名/检索族场景由 gate_node 兜）
    # 引号内是被转述的访客留言/说说正文，不算 narrator 自己的声称（20260913：
    # 留言板里那句"执行调用 navigate_to"被转述时误伤）
    own = _strip_quoted_spans(reply)
    if _state_action_claim(own, exec_memory):
        return ("state_claim_without_tool", _FALLBACK_STATE_CLAIM)
    if _site_search_claim(own, exec_memory):
        return ("search_claim_without_tool", _FALLBACK_SEARCH_CLAIM)
    if skill == "chat":
        if _chat_tool_claim(own):
            return ("claim_without_tool", _FALLBACK_CLAIM)
        return None
    if skill == "content_query":
        if (_READ_CLAIM_RE.search(own) or _EXECUTION_CLAIM_RE.search(own)
                or _CALLED_TOOL_CLAIM_RE.search(own)):
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
_FALLBACK_STATE_CLAIM = (
    "喵呜……主人，这一轮系统没有任何工具执行，页面和设置并没有真的改变——我刚才说"
    "『已经帮你打开了/关掉了』是不对的，只是嘴上说说。要我现在真的去执行吗？说一声"
    "我马上让系统动手喵。")
_FALLBACK_SEARCH_CLAIM = (
    "喵呜……主人，我得说实话：这一轮系统没有任何工具执行，我说的『翻了一遍/检索了"
    "一圈』是嘴上跑火车，没有依据。要不要我现在认认真真查一遍再回答你？这次每一条"
    "都带真实来源喵。")
_FALLBACK_NO_EXEC = (
    "喵呜……主人，我得纠正自己一句：这一轮系统**其实执行过工具**（只是返回是空的，"
    "没有查到东西），我刚才却说成『本轮没有执行任何工具』——把『查了但没有』讲成"
    "『压根没查』，这是我的错 :委屈: 要我再换一组关键词查一遍嘛？这次查到什么、"
    "没查到什么都如实告诉你喵。")

_FALLBACK_REPEAT = (
    "喵呜……主人，我刚刚差点把上一轮的回复原样再贴一遍——那样等于没回答你。这一轮"
    "我没有新东西可补充，就不复读了 :委屈: 你要我**重新查一遍**，还是想问我哪一点？"
    "说一声我马上照做喵。")

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

    20260920 修复：`fallback_text` 此前**未在 AgentState 声明**，LangGraph 只透出
    schema 里声明过的 key ⇒ 该字段被静默丢出 updates 流、server.py 的
    `upd.get("fallback_text")` 恒为假——`__RESET__` 从未发出、fallback 文本从未替换
    最终回复（20260903 起 2.5 周内所有 gate fallback 在 /chat/stream 与 golden 上
    全程失效）。后果不止"少一次替换"：被否定的叙述照常展示**并作为最终回复存入
    chat_history**，下一轮随历史注入又成为 narrator 自己的范文——20260920 实证四代
    克隆链，末两代一条 781 字回复与 11 小时前那条**逐字节相同**（见
    _REPEAT_MIN_RUN 注释）。声明见 AgentState 的 fallback_text 字段。
    """
    record("gate", "fallback", issue=issue, skill=plan["skill"], frames=frames)
    logger.info("[gate] fallback（%s）: skill=%s frames=%d", issue, plan["skill"], frames)
    return {"done": True,
            "messages": [SystemMessage(content=f"[Fallback 决定]: {text}")],
            "fallback_text": text}


# ---------------------------------------------------------------------------
# 3. Node：planner（唯一决策）/ execute（确定性执行）/ model（narrator）/ gate
# ---------------------------------------------------------------------------


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
    """
    if _stopped(config):
        logger.info("[planner] cancelled (client disconnected)")
        raise AgentCancelled()

    user_msg = _last_user_msg(state["messages"])
    page_ctx = _page_ctx(state["messages"])
    rounds = state.get("plan_rounds", 0)
    has_frames = _has_frames(state["messages"])
    doc_anchors = _doc_anchors(state["messages"])
    if rounds == 0:
        # 注入上下文留痕（20260919 D）：本轮 planner 实际看到的 page_ctx /
        # 节选 / 锚点清单落 trace——此前 trace 里没有这些，复盘"agent 到底看到
        # 了什么"只能靠日志反推（20260919 17:18 那轮就是靠 execution_log +
        # 逐条回复反推出来的）。只记首轮（三者不随轮次变），控体积。
        record("planner", "context", page_ctx=page_ctx[:1500],
               recent_tail=_recent_tail(state["messages"])[:900],
               short_reply=_short_reply_hint(state["messages"])[:400],
               doc_anchors=doc_anchors[:600])

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
    # 工具帧文本先算一次（下面 format 里要用，trace 里也要记长度）——20260920 起
    # 落 `frames_chars`：单帧上限 20000 是拍出来的经验值，没有真实体量数据就无法
    # 判断"该收该放"（超长文章改造后尤其要能看见节选是否生效）。
    frames_txt = _frame_texts(state["messages"])
    try:
        _prompt = _PLANNER_PROMPT.format(
            skills_context=build_planner_context(), tools_desc=_QUERY_TOOLS_DESC,
            page_ctx=page_ctx, round_info=round_info,
            intent_hints=_intent_hints(state.get("executed") or [], user_msg),
            doc_anchors=doc_anchors,
            recent_context=_recent_tail(state["messages"]),
            # 短应答提示只在首轮（rounds==0）给：第二轮起本轮已有工具帧，短应答
            # 的语义已由第一轮的规划兑现，再念一遍"把提议那件事规划出来"只会
            # 诱导重复规划（同一件事已经执行过一次了）。
            short_reply_hint=(_short_reply_hint(state["messages"]) if rounds == 0
                              else "（非首轮决策：短应答语义已在上轮兑现）"),
            tool_results=frames_txt,
            # 参数引用的可取值字段（规则 3b）——只列已成功执行且结构可解析的
            # 工具返回，模型照此写 $tool[0].field（见 agent/refs.py）
            ref_hints=ref_hints(state.get("tool_data") or []),
            reflector_feedback=state.get("issues") or "（本决策轮无复盘建议）",
            max_rounds=MAX_PLAN_ROUNDS, user_msg=user_msg)
        resp = llm.invoke(_prompt)
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
           frames_chars=len(frames_txt),
           **({"slow": True} if slow else {}))

    raw = getattr(resp, "content", str(resp))
    skill_name = re.search(r"SKILL\s*[:=]\s*(\w+)", raw, re.IGNORECASE)
    skill_name = skill_name.group(1) if skill_name else "chat"
    params = _parse_params(raw)
    plan_obj = instantiate_plan(skill_name, params)
    plan_obj["params"] = params

    # 白名单剔除可见化（20260913 B 项）：planner 点名了白名单外的工具时，条目被
    # instantiate_plan 剔除——此前无任何记录，planner 以为计划已执行、narrator
    # 照计划声称"我调用了 X"，agent.log 却查无此事（15:51 trace 实证：planner
    # 点名 get_social_links，被静默剔除后回复谎称"这次我用专门的社交链接查询工具
    # 调了一次"）。现在剔除即 WARNING + trace 事件，排障不再靠猜。
    if plan_obj.get("dropped"):
        logger.warning("[planner] 点名工具被白名单剔除（不会执行、无帧）：%s（round %d/%d）"
                       "——若属应支持的数据工具，检查 skills.py 白名单与菜单",
                       "、".join(plan_obj["dropped"]), rounds + 1, MAX_PLAN_ROUNDS)
        record("planner", "rejected_call", dropped=plan_obj["dropped"],
               skill=plan_obj["skill"], round=rounds)

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
            # 20260912：去重收尾前看意图清单——还有未完成动作时不得收尾（否则
            # 第二个意图就此丢失，正是 multi_intent 14% FAIL 的成因）。此时放行
            # 本轮计划让 planner 下一轮据清单继续（动作工具是显式 on/off 语义，
            # 重复执行幂等无害；宁可多跑一轮，不可丢用户要求）。
            pending = [i for i in _scan_action_intents(user_msg)
                       if not _intent_done(i, state.get("executed") or [])]
            if not pending:
                logger.info("[planner] 动作已执行（%s），去重收尾",
                            "、".join(sorted(planned_names)))
                plan_obj = _wrap_up_plan(True)
                return {"plan": plan_encode(plan_obj), "plan_rounds": rounds + 1,
                        "done": False}
            logger.info("[planner] 动作重复（%s）但意图清单仍有未完成项（%s）→ 不收尾",
                        "、".join(sorted(planned_names)),
                        "、".join(i["key"] for i in pending))

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
            if kind == "data_repeat":
                # 数据直取工具（无参数据工具/不带检索语义的调用）重复点名：同一份
                # 数据再取一遍零新信息，且没有"改读候选"这回事——直接收尾，让
                # narrator 基于上一轮帧如实作答（20260913 白名单补齐后实测：问社交
                # 链接，round 2 planner 重复 get_social_links，旧逻辑按"检索重复"
                # 拦下并给出与检索无关的"不得读无关文章顶替"注记）。
                logger.info("[planner] 数据工具重复拦截（%s）→ 直接收尾",
                            "、".join(dups))
                plan_obj = _wrap_up_plan(
                    True, "该数据工具本轮已执行过（数据已在上方工具返回里），"
                          "基于已有返回如实作答，不重复调用")
                record("planner", "intercept", reason=kind, dups=dups, redirected=False)
                return {"plan": plan_encode(plan_obj), "plan_rounds": rounds + 1,
                        "done": False}
            terms = _search_terms(plan_obj, executed, user_msg)
            cand = _candidate_detail_plan(state["messages"], executed, terms)
            if cand is None:
                logger.info("[planner] 检索重复拦截（%s），无可读候选 → 如实收尾列候选",
                            "、".join(dups) if dups else "rag_search 变体 ≥2 次")
                plan_obj = _wrap_up_plan(
                    True, "检索重复且候选无法确定目标（不得读无关文章顶替）")
            else:
                logger.info("[planner] 检索重复拦截（%s）→ 改读候选 %s",
                            "、".join(dups) if dups else "rag_search 变体 ≥2 次",
                            cand["tools"])
                plan_obj = cand
            record("planner", "intercept", reason=kind, dups=dups,
                   terms=sorted(terms), redirected=cand is not None)
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



def _search_retry_kind(plan_obj: dict, executed: list) -> str | None:
    """重复调用拦截判定（纯函数，20260905 工具级计数扩展，20260913 分出数据工具族）。

    content_query 轮 planner 计划中仍含 executed 里的同款 spec：
      - 重复项里有检索族工具（search_notes/rag_search）→ "retry_loop"（原句连发，
        20260903 判据）——调用方按"改读未读候选全文/如实收尾列候选"处理；
      - 重复项只有数据直取工具（无参站点信息类等）→ "data_repeat"：同一份数据再
        取一遍零新信息，但**没有候选可读**，调用方直接收尾如实作答（20260913
        白名单补齐后实测命中：round 2 重复 get_social_links）。
    无同款 spec 但本轮仍规划 rag_search 且已执行 rag_search ≥2 → "rag_loop"
    （换词变体打转——rule5"换词语义重试"已给足 2 次自由检索：首搜 + 一次换词，
    第三次变体在 BM25 下大概率仍回同批文档，判定打转）。都不中 → None（放行）。

    rag_loop 只统计 rag_search：search_notes/list_notes 是确定性点名列（成对点名/
    多关键词链合法），无打转实证；__ERROR__ 帧的修正重试由调用方整块跳过
    （本函数不看 messages）。
    """
    if plan_obj.get("skill") != "content_query" or not plan_obj.get("tools"):
        return None
    dups = [s for s in plan_obj["tools"] if s in executed]
    if dups:
        # search_notes 计检索族（与 rag_search 同属"换词再搜"语义，重复即打转）；
        # get_article_detail/list_notes 等不算——重复读同一篇/列同一页由
        # data_repeat 兜底收尾（不误判成检索打转、不触发候选改读）
        return ("retry_loop" if any(_tool_name(s) in ("search_notes", "rag_search")
                                    for s in dups) else "data_repeat")
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


def _check_spec(name: str, args: dict, args_ok: bool, raw: str, skill: str,
                kind: str = "ok") -> tuple[str, str]:
    """checker 确定性验收（20260904，execute 循环内逐 spec 调用，无 LLM）。

    输入 = spec 实际调用值（args 是文案注入后值）+ 工具原始返回 + 返回值的 kind
    （20260916 起：ok / empty / unavailable，见 tools/base.py 的 ToolResult）。
    只做回执形态校验（错误帧/空结果/服务不可用/命令帧形状），不做文本语义判断——
    语义由 planner 从帧里自己读（错误修正重试是 planner rule5 的活）。
    PASS → 该执行成为系统确认事实（receipts，跨轮执行记忆原料）；
    BLOCK → 该执行不进回执（错误结果不是事实），进 blocked 交 planner/reflector。

    kind=unavailable 单独判 BLOCK：**"服务挂了"不是事实**，不能进跨轮执行记忆
    （否则下轮质疑"你刚才查到了什么"时，agent 会照着一条故障回执编）。kind=empty
    仍走 PASS——"查到了，就是空的"本身是事实。
    """
    if name not in _TOOL_MAP:
        # 白名单结构上到不了 execute，防御保留（执行器不静默吞越权）
        return _VERDICT_BLOCK, "unknown_tool"
    if not args_ok:
        return _VERDICT_BLOCK, "args_parse"
    if kind == "unavailable":
        return _VERDICT_BLOCK, "unavailable"
    text = raw or ""
    if not text.strip():
        return _VERDICT_BLOCK, "empty_result"
    if text.lstrip().startswith("__ERROR__"):
        # 参数引用失败单独给原因码（20260919）：planner 要按"是路径错还是没执行过"
        # 分别改参/换路，笼统的 error_frame 给不出这个信息。
        # 权限拒绝同办（20260920）：scope_denied 是"你的身份不允许"，与"工具报错"
        # 要分开——planner 的应对是如实告知，不是换个工具再试。
        # 未获确认的写操作同办（20260920）：consent_required 是"还没问过用户"，
        # planner 的应对是去问，而不是当成"做不到"。
        return _VERDICT_BLOCK, (ref_error_reason(text) or authz.scope_error_reason(text)
                                or authz.consent_error_reason(text) or "error_frame")
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
    principal = _principal_of(config)  # 本轮调用者（权限判据的输入，见 agent/authz.py）
    user_msg = _last_user_msg(state["messages"])
    page_ctx = _page_ctx(state["messages"])
    results: list = []
    receipts = list(state.get("receipts") or [])  # 请求内累计（与 executed 同模式）
    blocked: list = []                            # 只含本轮受阻项（路由/reflector 用）
    prev_seen = set(state.get("blocked_seen") or [])  # 本轮之前的受阻 spec 集
    tool_data = list(state.get("tool_data") or [])    # 参数引用的取值源（请求内累计）
    for idx, spec in enumerate(specs):
        if _stopped(config):
            # 逐 spec 检查（20260916 补）：入口那一次只能拦住"整份清单还没开始执行"。
            # 多写操作清单（如 [navigate_to, device_oled_display]）在中途断连时，剩下的
            # 写操作会照单执行完——与本模块承诺的"写操作绝不发生在用户已离开之后"不符。
            # 这里 break 而不是 raise：**已经执行完的 spec 的回执必须留下**（那是真发生
            # 过的事实，raise 会把 receipts 一起丢掉，回执正是跨轮执行记忆的原料）。
            # 未执行的 spec 也不进 blocked——planner 下一轮在入口就被取消检查拦下。
            logger.info("[execute] cancelled mid-plan — 剩余 %d 个 spec 不执行（已处理 %d 个）",
                        len(specs) - idx, idx)
            break
        name = _tool_name(spec)
        args, args_ok = _tool_args(spec)
        # 参数引用（20260919，agent/refs.py）：把上一步的真实返回值绑进参数。
        # 解析失败 → 该 spec **不执行**（拿 `$x[0].y` 当参数去调用是更坏的结果），
        # 产带原因码的 __ERROR__ 帧走既有 blocked 链路（planner 改参 → reflector）。
        ref_err = None
        if args_ok:
            args, ref_err = resolve_args(args, tool_data)
            if ref_err:
                args = {}  # 参数清单本身解析没问题（args_ok 保持 True）——失败的是取值
        tool = _TOOL_MAP.get(name)
        # 权限判据（20260920，秘书类功能地基）：唯一判据点 = 调用之前，与断连检查、
        # 参数引用解析同一层（确定性、无 LLM、无一例外）。默认 shadow——
        # 只算决策、只把**拒绝**记进 trace，行为不变（先观测、后收口，见 agent/authz.py）。
        decision = authz.check(principal, name)
        if not decision.allowed and not authz.enforcing():
            record("execute", "authz_shadow", tool=name, principal=str(principal),
                   decision=str(decision))
        # 写操作的「人在回路」确认（20260920，秘书类前置需求 ③）：**权限判"能不能做"，
        # 这里判"这一次用户到底要不要做"**。只对有 CONSENT_SCOPES 声明（写站点内容、
        # 对外可见收不回）的工具生效——今天没有这类工具，所以对现有行为零影响；
        # 一旦新增，它**自动**落在闸下（声明驱动，不靠人记得来改）。与权限判据同层：
        # 确定性、无 LLM、调用之前、fail-closed。今天不设 shadow：这一层是纯新增的
        # 保护，不存在"真流量会被它改行为"的观测需求（没有工具会命中它）。
        consent_missing = (authz.requires_consent(principal, name)
                           and not authz.consent_granted(principal, name, user_msg))
        if consent_missing:
            record("execute", "consent_required", tool=name, principal=str(principal),
                   scope=authz.required_scope(name))
            logger.info("[execute] 写操作未经确认，不执行: %s（principal=%s）",
                        spec, principal)
        # 屏幕文案创作：text 参数缺失/为空 → execute 结合对话创作（技能固有设计）
        if ref_err is None and name == "device_oled_display" and not args.get("text"):
            args = dict(args)
            args["text"] = _create_display_text(user_msg, page_ctx)
        _t_tool = time.monotonic()
        if ref_err:
            out = f"__ERROR__: 参数引用无法解析[{ref_err}]（上一步返回里没有这个值——改参数或换个工具）"
            logger.warning("[execute] 参数引用解析失败，不执行: %s → %s", spec, ref_err)
        elif not decision.allowed and authz.enforcing():
            out = authz.denial_frame(decision, principal)
            logger.warning("[execute] 权限拒绝，不执行: %s → %s", spec, decision)
        elif consent_missing:
            # 与权限拒绝同族（__ERROR__ + 原因码 → blocked 链路），语义是"去问用户"
            out = authz.consent_frame(name, principal)
        elif tool is None:
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
        # 结构化返回值入 tool_data（引用取值源）：帧文本是给人看的（还截断），
        # 引用要走结构。解析不出 → data=None（引用它时报 ref_unparsed，不猜）。
        tool_data.append({"tool": name, "data": parse_data(str(out)),
                          "round": state.get("plan_rounds", 0)})
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
        # kind：工具自己声明的"两类"（ok/empty/unavailable，见 tools/base.py 的
        # ToolResult）。命令帧与 __ERROR__ 帧是纯字符串 → 默认 ok，由形态校验兜。
        verdict, reason = _check_spec(name, args, args_ok, str(out), plan["skill"],
                                      getattr(out, "kind", "ok"))
        if verdict == _VERDICT_PASS:
            rcpt = {"skill": plan["skill"], "tool": name,
                    "args": {k: str(v)[:200] for k, v in args.items()},
                    "result": str(out)[:200], "ts": time.time()}
            # 实体摘要（20260920，见 agent/entities.py）：数据工具取回的条目/计数
            # 压成一行随回执落 execution_log —— 工具帧只活当轮，不落这一行的话
            # 下轮「第二条写了什么」只能把工具再跑一遍（探针实测）。
            digest = receipt_digest(name, str(out))
            if digest:
                rcpt["digest"] = digest
            if name == "get_article_detail":
                # 跨轮执行记忆带标题（20260912）：下轮"那篇讲架构的"要靠它核对指代
                rcpt["title"] = _doc_title(str(out))
            receipts.append(rcpt)
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
               "blocked_repeat": repeat, "tool_data": tool_data}
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
   或建议用户稍后再问。**反向同样要如实**：记录里出现"返回（已执行，结果为空）"
   或"本轮执行回执"里有该次调用 = **执行过了**，只是没查到结果——不得说成"本轮
   没有执行工具/没有检索/回执为空"（20260920 真实事故：把空结果讲成没执行，
   访客的肯定应答被吞掉，还被反问"要不要我查一遍"）。空结果就如实说"查了，没有"。
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
    与连续正文段落不强行加粗。
14. 工具返回帧标注"超单帧上限，已按小节节选"（20260920）：这一篇**只有帧里
    展开的那几节**在记录里；文末「以下小节尚未展开」列出的节**没有读到**——
    不得引用其中的内容，也不得声称"全文都看过了"。被问到的正是未展开的小节时
    如实说那一节我这轮没读到、可以按小节名再取一次（系统下一轮会读回），
    绝不拿相近小节的内容顶替作答。
15. 指代不唯一时先追问，不替访客挑一个（20260921）：访客用"那个分类""那篇"这类
    指代，而依据里并列着**多个同类候选**（页面上下文 recent_executions= 的实体
    摘要形如"5 个分类: 测试 5 篇/…/编程 8 篇"——并列命名、无序号）→ **先问清是
    哪一项**再答，不得默认挑第一个/最多的那个（挑错了访客看不出来你在猜）。
    追问要**点名候选**（"测试、本项目介绍、摄影、编程、Web3 里的哪一个？"）。
    反面同样要守住：摘要里**带序号**的条目（"最近3条: 1.…/2.…/3.…"）"第二条"是
    唯一的，直接照抄取值；访客已点名（"编程那个分类"）也直接答——这两种情况**反问
    就是多此一举**。"""


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

    # ── 1b. 逐字复读上一轮回复（任何轮次，20260920，见 _REPEAT_MIN_RUN 注释）──
    # 排在 2/3（声称/URL）之前：复读是**整段照抄**，比它夹带的单句声称更该先报——
    # 否则一条复读里的旧声称会按**本轮**帧去判，issue 名报成编造而非复读，把
    # "抄了自己"这个真信号淹掉（00:23:52 那条就是被记成 phantom_search_claim）。
    # 用户点名要求重做/重发（_REDO_REQUEST_RE）时判据自行放行——重合是被要求的。
    prev_reply = _prev_ai_reply(msgs)
    if _repeat_of_prev_reply(reply, prev_reply, _last_user_msg(msgs)):
        logger.info("[gate] 回复逐字复读上一轮（本轮 %d 字 / 上轮 %d 字，门槛 %d）→ fallback",
                    len(reply), len(prev_reply),
                    max(_REPEAT_MIN_RUN, int(len(reply) * _REPEAT_COVER)))
        return _fallback_result("repeat_prev_reply", _FALLBACK_REPEAT, plan, len(frames))

    # ── 2. 命令前缀文本（任何轮次，正文出现命令帧前缀 = 假装发命令）─────────
    # ── 3. 编造资源 URL（任何轮次，工具返回/用户消息中不存在的 /api 或图片）──
    issue = _claim_issue(reply, plan["skill"], plan, bool(frames), _has_exec_memory(msgs))
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
        if _COMPLETION_CLAIM_RE.search(reply) or _WRITE_CONTENT_CLAIM_RE.search(reply):
            logger.info("[gate] 工具帧 __ERROR__ 但回复含完成式声称 → fallback")
            return _fallback_result("err_frame_claim", _FALLBACK_ERR_CLAIM, plan, len(frames))
    # 5b. 确认式导航（NAVIGATE: 帧、无 AUTO_NAVIGATE:）却回复到达声称 →
    #     页面实际未跳转（前端等确认）
    if plan["skill"] == "navigate" and "NAVIGATE:" in tool_text and "AUTO_NAVIGATE:" not in tool_text:
        if _NAV_ARRIVAL_RE.search(reply):
            logger.info("[gate] NAVIGATE 确认帧 + 到达声称 → fallback")
            return _fallback_result("nav_pending_claim", _FALLBACK_NAV_PENDING, plan, len(frames))
    # 5c. 具名工具声称（20260913 C 项）：有帧 ≠ 帧里有那个工具——回复第一人称
    #     完成式点名"我调用了 X"而 X 本轮没执行（越权被剥/被跳过）= 编造调用
    #     （15:51 实证句："这次我用专门的社交链接查询工具（get_social_links）调了一次"）
    executed_names = {str(getattr(m, "name", "") or "") for m in frames}
    phantom = _phantom_tool_claim(reply, executed_names, _has_exec_memory(msgs))
    if phantom:
        logger.info("[gate] 具名工具声称无帧支撑：%s（本轮执行=%s）→ fallback",
                    phantom, "、".join(sorted(n for n in executed_names if n)) or "无")
        record("gate", "phantom_tool_claim", tool=phantom,
               executed=sorted(n for n in executed_names if n))
        return _fallback_result("phantom_tool_claim", _FALLBACK_CLAIM, plan, len(frames))

    # 5d. 站内检索声称 vs 本轮内容类帧（20260919 gate 洞②的混合轮形态）：回复说
    #     "我检索了一圈/把站内翻了一遍/用 rag_search 搜了一遍"，而本轮**一个内容类
    #     工具都没跑**（只跑了导航/特效/设备这类动作工具）→ 检索声称无据。5c 只管
    #     点名工具，泛指检索声称归这里。
    if not (executed_names & _CONTENT_TOOLS):
        own5d = _strip_quoted_spans(reply)
        if _site_search_claim(own5d, _has_exec_memory(msgs)):
            logger.info("[gate] 站内检索声称但本轮无内容类工具帧（执行=%s）→ fallback",
                        "、".join(sorted(n for n in executed_names if n)) or "无")
            record("gate", "phantom_search_claim",
                   executed=sorted(n for n in executed_names if n))
            return _fallback_result("phantom_search_claim", _FALLBACK_SEARCH_CLAIM,
                                    plan, len(frames))

    # 5e. 假阴性声称（20260920 洞③）：本轮**真执行过**（有已验证回执）却宣称"本轮
    #     没有执行任何工具/回执为空"——与 5c/5d 反向，把"查了但没有结果"讲成"没查"，
    #     访客的肯定应答被吞掉（真实 trace 20260920 00:56:23 的确认死循环）。
    if _false_negative_claim(_strip_quoted_spans(reply), bool(state.get("receipts"))):
        logger.info("[gate] 回复谎称本轮未执行但回执在场（receipts=%d）→ fallback",
                    len(state.get("receipts") or []))
        record("gate", "false_negative_claim",
               receipts=len(state.get("receipts") or []))
        return _fallback_result("false_negative_claim", _FALLBACK_NO_EXEC,
                                plan, len(frames))

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
            "reflect_end": False, "tool_data": [], "fallback_text": ""}
