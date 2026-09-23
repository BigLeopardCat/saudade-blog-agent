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


# 本文件**不要**加 `from __future__ import annotations`（20260920 实测踩过）：
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
from agent import adminops as A
from agent import authz
from agent import confirm
from agent import refs
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
from agent.prompts import BLOG_ASSISTANT_PROMPT, STICKER_GUIDE, audience_block
from agent.refs import parse_data, ref_error_reason, ref_hints, resolve_args
from agent.skills import (FUZZY_NAV_RULES, NAV_MAP, SKILL_MAP,
                          _CALLABLE_QUERY_TOOLS_ORDER, _WRITE_NAME_TARGET_SKILLS,
                          build_planner_context, instantiate_plan, visible_skills)
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

# 快照型只读技能（20260921 管理助手）：一次取回、零依赖、返回的是**当时**的读数。
# 与 content_query 的多轮检索不同（换关键词再搜是新信息），这类技能第二轮起
# 再规划拿的是同一份数据（CPU 会重采，但那是噪声不是新信息）。实测
# ops_report_admin 连规划 4 轮 = 同两个工具各跑 4 遍（22s，工具 8 次），
# 病灶与"数据工具重复拦截"（content_query 族）相同，只是报表技能不经那条通道。
SNAPSHOT_SKILLS = frozenset({"ops_report", "moderation_report", "user_report"})

# 后台写技能（20260921 第二轮）：**不是**快照型——见 planner 里的重复规划防护。
# 20260921 第三轮评估（新写技能 tag_update/tag_delete/category_* 要不要进来）→ **不进**。
# 判据是"同一件事已经做过"的正确性而不是省一轮：这三族里"再做一次"是**正常请求**
# （改回原名、换个颜色、再删一个），而进来的效果是 planner 见到逐字相同的计划就收尾；
# 收尾话术再诚实也挡不住一种情形——用户在后台把那件事改回去了、再让 agent 做，agent
# 只会说"刚做过"。宁可多规划一轮让工具去活字典里核一遍（核不到会响亮地报出来），
# 也不要把"没做"说成"做过"。tag_create 留在里面是因为它**幂等**（同名复用是真的同一件事）。
EXECUTED_ONCE_SKILLS = frozenset({"tag_create", "article_status", "article_tags"})

# 需要"本轮读过这个 id 才准写"的工具（见 execute 的目标校验）。
# 20260923 批 7 加入收藏两件：它们的目标同样是 article_id，同样会**写错篇**
# （收藏/取消收藏到用户没说的那一篇），而误靶的来源一模一样（planner 拿列表首行
# 当用户点名的那一篇）。差别只在影响面小（可逆、不外显）——不影响判据要不要。
_ARTICLE_WRITE_TOOLS = frozenset({"set_article_status", "set_article_tags",
                                  "add_favorite", "remove_favorite"})

# 弹窗问句里要写《标题》的工具（同 _ARTICLE_WRITE_TOOLS 减去收藏两件）：
# 标题来自后台文章清单（`_note_index` → `/api/protected/notes/list`，**管理员面**），
# 而收藏是普通访客也能用的（scope=write.own 三档角色都有）——对访客读这一份清单
# 只会拿到一次 403。判据放在"这个人能不能读后台清单"上（authz.check），
# **不是**"这个工具要不要弹窗"：读不到就退回只写 id（同全表取向，绝不因此不弹窗）。
_POPUP_TITLE_TOOLS = frozenset({"set_article_status", "set_article_tags"})

# 写工具回执里允许进 meta 的键（白名单，防止工具侧随手加的键悄悄进生产库 detail；
# 消费端 Rust 只认这几个，多出来的键是无声的兼容性债）。
# `tag_name` 是**标签名**不是文章标题——写行刻意不带《文章标题》（它会被下一轮读成
# "我读过这篇"的指代证据），标签名没有这个歧义，且"新建了哪个标签"必须记下来。
# `change` 是写操作的**变更摘要**（"改名为 X、颜色改为 粉色"），`category_name`
# 同理是分类名（分类没有会漂的 id 语义，名字就是用户认得的那个）。
_RCPT_META_KEYS = ("op", "article_id", "before", "after",
                   "tag_id", "tag_name", "level",
                   "category_id", "category_name", "change",
                   "announcement_id", "announcement_title",
                   "board_id", "board_author")

# 目标证据的来源工具：本轮帧里**真带 note id** 的那几个（公开列表/检索/详情、
# 后台列表、置顶列表）。刻意不含写工具自身的回显（"刚刚写过 id=12"不能成为
# "可以再写一次 id=12"的依据——那会把整个校验自我豁免掉）。
_TARGET_EVIDENCE_TOOLS = frozenset({
    "get_article_detail", "list_admin_notes", "list_notes", "search_notes",
    "rag_search", "get_top_notes",
    # 自己的收藏列表（20260923 批 7）：它的行里带 noteId，是"取消收藏那一篇"
    # 唯一可能的来源帧（用户说「把收藏里的《X》取消掉」时 planner 必须先读它）。
    # 与上面几个一样只是**材料**来源，不代表 id 一定对（那是 target_named 的事）。
    "list_my_favorites",
})


def _target_evidence(state, user_msg: str, page_ctx: str) -> list[str]:
    """本轮可见的、可能承载文章 id 的材料（目标校验的输入，纯拼装无判断）。"""
    texts = [user_msg or "", page_ctx or ""]
    for m in state.get("messages") or []:
        if (isinstance(m, ToolMessage)
                and (getattr(m, "name", "") or "") in _TARGET_EVIDENCE_TOOLS):
            texts.append(str(getattr(m, "content", "")))
    return texts


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
    # ── 写操作确认弹窗（20260921，同样**必须显式声明**，理由同 fallback_text）──
    # pending_confirm: 本轮要弹的确认框（{q, opts, token, specs, skill}）——
    #                 execute 判定"有意向但没判成命令"时写入，随后路由直接 END
    #                 （不跑 narrator：这一轮什么都没执行，跑叙述只会让它有机会
    #                 说"已经建好啦"，而这正是 gate 一直在打的地鼠）。
    # confirm_text:   弹窗那一轮的**回复正文**（确定性中文问句，不经 LLM）。
    # confirm_grant:  隐藏确认请求带进来的已验签 payload（server.py 验签后注入）——
    #                 planner 见它走确定性短路径（不再花一次 LLM 决策），
    #                 execute 见它放行同意闸与目标有据两门。
    pending_confirm: dict
    confirm_text: str
    confirm_grant: dict


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

短应答提示（当前消息只是"要/好/不用了/算了/你看着办"这类短应答时，这里给出它所
承接的上一轮泠月发言与判定方向；不是短应答则为缺省语）：
{short_reply_hint}

{tool_results}

本轮已执行工具的**可引用字段**（参数引用的取值来源，见规则 3b——字段名照抄，
路径只能从这里列出的键名前缀往下写，不许臆造）：
{ref_hints}

复盘建议（reflector 对重复受阻项的 ISSUE 修正指引——仅当上一轮复盘判 replan
后才有内容；没有则为缺省语，按常规规则决策）：
{reflector_feedback}

系统纠偏（确定性事实——只在你上一版决策**不可用**时才有内容（点名的工具全被剔除，
或主人在原话里点名了目标、你却没写出任何工具规格）；正常决策轮是缺省语。有内容时按
它重新决策：里面的工具归属是系统从技能注册表读出来的，不是猜测）：
{correction}

判定规则：
1. 决策类型（SKILL）：
   - **短应答先还原语义**：消息只是"要/好/可以/不用了/算了/你看着办"这类短应答时
     （上方短应答提示会点明），它**不是新话题**——含义由上一轮泠月的发言决定：同意/
     要求继续 → 把泠月提议的那件事真的规划出来执行（该点名的工具照常点名），
     不得只口头答应；拒绝/收回 → 本轮零调用收尾，简短确认不做，不得再执行那个
     动作也不得声称做了什么。禁止拿短应答去检索或答别的内容。
   - **授权式**（"按你想法来吧/你看着办/都行"，短应答提示会标出来）：主人把
     「做哪一件」也交给了你 ⇒ 目标只能从**系统数据**里定（本轮待办/待审清单、
     上一轮泠月点过名的那件事、工具回执），**不许从历史对话里挑一条自然语言当
     目标**；唯一候选就照常规划执行，候选不唯一/查不到 → 零写、如实列出候选请
     主人点名，绝不替主人选，也绝不说"已经发起/已经确认"。
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
4b. 写操作纪律（新建标签 / 改文章状态 / 加去标签等各类写操作，**仅管理员**）：
   - **参数取值一律从主人这句话里原样抄**：技能描述与判据里的〈…〉**全是
     占位符**，一个都不许进参数。名字形态奇特（下划线/数字/英文代号/带空格）、
     或一句话里同时出现"父标签"和"新名字"时最易出错——把示例占位符、句子里的
     功能词、或名字的半个片段当成取值填进去，**弹窗就会问一个主人从没说过的
     名字**。填完逐个参数对着原话核一遍：这个值，主人真的说过吗？说过的名字
     要**完整照抄**（不许截断、不许去下划线、不许拆成两截分给两个参数）。
   - **"要不要执行"不由你判断**：主人点名了对象与动作（"把文章 12 设为私密"
     "去掉「X」标签"），哪怕措辞不标准、哪怕你觉得是破坏性操作，都**照常
     输出该技能的 TOOLS**——"要不要真动手"由系统在**确认框**上问主人（执行器
     里的同意闸），不是你在这里替他决定。你替它决定＝那一轮什么都不会发生。
   - **禁止**用 chat 收尾去索要"再确认一句/回复『执行：…』我就去做"：这既多烧
     一轮对话，又**不会弹确认框**（确认框由系统在"计划里真有写操作"时才弹）。
     上一轮工具帧里已经有 id / 标签名、主人这句又是命令式 → 直接规划写操作。
   - 只有两种情形**不**产出写 spec：①主人在**提问或假设**（"如果设为私密会怎样"）；
     ②写目标与站内数据对不上（清单里根本没有那一篇/那个标签）——此时先读清单
     定位，而不是硬写。
   - 帧里返回 `[target_mismatch]`（改的篇与主人点名的不一致）→ 按帧里给出的 id
     改回来，下一轮用主人点名的那个 id 重新规划；不得反驳、也不得将错就错。
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
    # 两级一起给（20260921 修）：只写"一级标签"时 planner 会照菜单作答"站内没有
    # 二级标签"——工具早就读得到两级了，缺的只是菜单没说
    "list_tags": "无参直取：全部标签（一级 + 其下的二级，靠 level/fatherTag 区分）",
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


def _drop_correction(dropped: list[str], role: str | None) -> str:
    """剔空纠偏提示（确定性文本，零 LLM）：planner 点名的工具一个都没执行时，
    把"这些工具在哪条通道上"写给它看，由它重新决策。

    **为什么要有这一条**（20260921 22:34 生产实证）：管理员问「小猫咪那篇文章都有
    什么标签呀」——那篇是草稿，公开接口看不见，planner 点名 `list_admin_notes`
    **是对路的意图**，但该工具属于 `admin_notes` **技能**、不在 content_query 的
    calls 白名单里 ⇒ 清单被剔空 ⇒ 旧行为当收尾轮处理 ⇒ narrator 零帧编话 ⇒ gate
    打回 ⇒ 用户看到降级回复且本轮直接结束（用户原话："居然就直接结束而不是重新
    规划执行"）。纠偏文本只写机器能保证的事实（工具归属从注册表读、可见性走
    `visible_skills(role)` 这唯一一处角色判据），**不替 planner 选技能、不猜用户意图**。
    """
    lines = ["**你上一版决策点名的工具一个都没有执行**（不在你这个身份可点名的调用"
             "清单里，本轮零工具、零结果）。逐个说明："]
    for raw_name in dropped:
        name = str(raw_name).split("（", 1)[0].strip()   # 去掉"（args 非对象）"后缀
        suffix = str(raw_name)[len(name):]
        if suffix:
            # 带后缀 = 工具没问题、是**这条例目**不合法（args 不是对象）——不能
            # 说成"你够不到这个工具"（那是假的，会把 planner 往错方向推）
            lines.append(f"- {name}：工具本身你可以调用，但这条例目不合法{suffix}"
                         f"——args 要写成 JSON 对象（键值对），别写成字符串")
            continue
        if name not in _TOOL_MAP:
            lines.append(f"- {name}：站内**没有**这个工具（工具名必须来自上方清单，不许臆造）")
            continue
        owners = [s.name for s in visible_skills(role)
                  if any(t == name for t, _ in (s.plan or ()))]
        if owners:
            lines.append(f"- {name}：它属于技能 {'、'.join(owners)} —— 要用它请把 SKILL "
                         f"选成那个技能（技能模板会自动带上它），**不要**写进 PARAMS.calls")
        else:
            lines.append(f"- {name}：你够不到这个工具——本轮你的身份没有任何可用技能"
                         f"会用到它，它也不在可点名的查询清单里（需要管理员身份的通道"
                         f"不会列给当前身份）")
    lines.append("请重新决策：改用清单里合适的工具或上面点明的技能；确实查不了就"
                 " SKILL=chat 如实说明查不到。**不许**说「查过/看过/读过/调用过」——"
                 "本轮确实什么都没执行。")
    return "\n".join(lines)


# ── "主人点名了目标，你却没写工具规格"：确定性纠偏一次（②防线续二）──────────
# 实测（20260922 golden `admin_tag_move_unresolved_target_honest` 八跑）：同一条
# 「把标签「绝对不存在的标签名xyz」挪到「编程」下面」有 2/8 跑出**零工具**——
# planner 写下"不确定站内有没有这个名字，先问主人"，于是这一轮什么都不发生：主人
# 原地重述自己刚说过的话，而系统那套"站内到底有没有这个名字"的台账核对**压根没跑**
# （`_write_target_refusal` 只在有工具规格时才判，见其头注）。
# 技能描述里那句「命令式措辞即便你觉得该先问一句，也照常选本技能——要不要真动手由
# 系统弹确认框问主人」**早已写在那儿**，它照样这么干 ⇒ 一句话劝不动，得给一次确定性
# 纠偏（做法同 `_drop_correction`：只写机器能保证的事实 + 讲清"这不是你该预判的"，
# 重选仍由 planner 自己做）。
# 触发刻意收窄：首轮、零工具、无剔除、主人原话里有引号指认、不是提问/假设、且带
# 写域动作词——闲聊与问答（「「李白」写过什么诗」）结构上命不中。
_NAME_WRITE_VERBS = ("挪", "移到", "移动到", "挪到", "挂到", "换到", "放到",
                     "改名叫", "改名为", "改名", "改成", "换成",
                     "删掉", "删除", "去掉", "移除", "取消",
                     "新建", "创建", "建立", "新增")


def _name_write_nudge(plan_obj: dict, user_msg, rounds: int,
                      role: str | None) -> str | None:
    """写形态的请求上 planner 一条工具规格都没写 → 纠偏提示文本（见上方长注）。"""
    if rounds or (plan_obj.get("tools") or []) or plan_obj.get("dropped"):
        return None
    text = str(user_msg or "")
    spans = _msg_quote_spans(text)
    if not spans or authz.is_question_like(text):
        return None
    if not any(v in text for v in _NAME_WRITE_VERBS):
        return None
    # 角色判据只走 visible_skills 这一处（同 _drop_correction）：当前身份连一个
    # 名字通道写技能都看不到时（非管理员），纠偏只会把它往够不到的方向推。
    if not any(s.name in _WRITE_NAME_TARGET_SKILLS for s in visible_skills(role)):
        return None
    return (
        "**主人在原话里已经用引号点名了目标**："
        + "、".join(f"「{s}」" for s in spans[:3])
        + "。你这一版没有产出任何工具规格。\n"
        "如果你是因为『不确定站内有没有这个名字 / 这件事做不做得成』而打算先问主人"
        "——**那不是你该预判的事**：名字落不到唯一一行、或者站里本来就没有这个名字，"
        "系统会照着站内台账**如实回话**（并写明本轮零执行、站内数据一个字节都没改）。"
        "你要做的是**照主人的原话把工具规格写出来**（SKILL 选对、目标名字就抄主人引号里"
        "那一段，一个字都不要改写或截短），要不要真动手、影响面多大，由系统弹确认框"
        "问主人。\n"
        "（反过来：如果主人这句话本来就不是要改动站内数据的请求——只是提问、闲聊，"
        "或是要你解释/整理某段内容——那保持你现在的决定即可，不必强行凑一个写操作。）"
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
    # 20260921 后台写（管理助手第二轮）：**刻意不把新写动词放进这一族**。这一族
    # 不看完成标记、也不看疑问语气（历史设计，只在 content_query 零帧的异常轮宽查
    # 用）；放进来实测会把"文章 12 是已经置顶了吗？""请问是不是已经设为私密了？"
    # 这类**合法反问**判成声称（`已(?:经)?置顶` 后面跟的"了吗"它不管），而这两个
    # 场景里后台写域已有更准的两张网：**err 帧轮**走 5a 的 _WRITE_CONTENT_CLAIM_RE
    # （带完成标记 + 疑问豁免）、**零帧轮**走洞① 的 _STATE_ACTION_CLAIM_RE ④支
    # （子句级豁免表含 吗/呢/吧/？）——各场景一张网，重复挂只会多一处误伤面。
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
    # ①②③ 共用末尾那一个完成标记（原有语义逐字不变）。
    r"(?:"
    r"(?:帮你|给你|为你|替你|帮主人)(?:把)?[^\n。！？!?；;，,]{0,12}?"
    r"(?:发布|发表|投稿|提交|上传|发出|发送"
    # 20260921 后台写动词（管理助手第二轮）：未获确认时 narrator 最可能的说法是
    # "已经帮你建好标签啦/帮你把它置顶了"，动词表里没有就全漏。
    r"|置顶|取消置顶|隐藏|下架|设为私密|设为公开|设为草稿|设成私密|设成公开|设成草稿"
    r"|新建|创建|建好|打上|加上|去掉)"
    # 裸式只认 发布/发表 + 本轮的**后台写动词**：仍不含 提交（见上方注释里那句
    # "你已经提交过河灯啦"）。"标签已经建好啦"走的正是这一支。
    r"|(?:已经?|刚刚)(?:发布|发表|置顶|取消置顶|隐藏|下架|设为私密|设为公开|设为草稿"
    r"|设成私密|设成公开|设成草稿|新建|创建|建好|打上|加上|去掉)"
    r"|成功(?:发布|发表|投稿|提交|上传|发出|发送|置顶|隐藏|创建|新建)"
    r")"
    # 完成标记：区分声称与提议。裸"了"要排除**时长用法**——"这篇文章已经置顶了很久
    # 没动过"里的"了"是持续时长（不是完成态），实测被 ② 支配上这个标记判成声称；
    # 真完成式仍有后一个"了"可落（"已经置顶了很久了"照样命中）。
    # 疑问豁免提到**外层**（20260921，原只挂在 ④ 支）：实测 ② 支命中的
    # "文章 12 是已经置顶了吗？""标签已经建好了吗？""请问文章 12 是不是已经设为私密了？"
    # 都是**疑问语气**（完成标记后紧跟 吗/呢/吧/？），却因 ② 支没有豁免被判成声称。
    # 这一支在本轮尤其要紧——consent 未过时 narrator 的**正解就是反问**（问主人
    # "是不是已经…了"），而门恰好在"有 __ERROR__ 帧 + 完成式声称"时触发；把正解
    # 判成谎称 = 整轮换成兜底道歉（本仓一贯的取向：这一步的误伤成本 > 漏拦）。
    # 历史影响为零：516 条真实 trace 复扫，W/C 既有命中集合里没有"标记后紧跟
    # 吗/呢/吧/？"的句子（见 test_authz.py ⑨b 的成对表）。
    r"[^\n。！？!?；;，,]{0,4}?(?:了(?![多久很])|啦|好|完成|成功|完毕)(?![吗呢吧]|[?？])"
    # ④ 远距离式（20260921 补）：**没有**施事前缀的"已经把它设为私密了"是未获确认时
    #    最自然的编造口吻，①②③ 都抓不到（① 要"帮你"，② 要动词紧跟"已/刚刚"）。
    #    动词**只放后台写域**（不碰 发布/提交）——"你已经把它提交过啦"那种转述主人
    #    过往动作的句子正是 ② 刻意挡开的坑，把通用动词放进这一支等于把坑挖回来。
    #    ④ **自带**完成标记 + 疑问豁免：本轮的写轮里 narrator 问主人"您是已经把
    #    文章 12 设为私密了吗？"是**合法追问**（consent 未过时最正确的收尾就是问），
    #    不该被当成声称。这一支是**独立备选**、自带标记，故豁免必须两处都挂
    #    （外层那份管 ①②③，这份管 ④；改动时漏掉任一处，疑问式就在那一支漏网——
    #    实测教训）。
    #    唯二不走 ② 的形态各有一条实测依据：`已经把它设为私密了`（有"它"无"把"）、
    #    `刚刚把标签加上了`（动词离"刚刚"三个字，且 加上 只在 ① 里）。
    r"|(?:已经?|刚刚)[^\n。！？!?；;，,]{0,12}?"
    r"(?:置顶|取消置顶|隐藏|下架|设为私密|设为公开|设为草稿|设成私密|设成公开|设成草稿"
    r"|改成私密|改成公开|改成草稿|新建|创建|建好|打上|加上|去掉)"
    r"[^\n。！？!?；;，,]{0,4}?(?:了(?![多久很])|啦|好|完成|成功|完毕)(?![吗呢吧]|[?？])"
    r")"
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
    # 20260921 后台写（管理助手第二轮）：置顶/隐藏/建标签同样只进这一支，理由相同。
    # 零工具写轮（缺参守卫回退、planner 只追问）里 narrator 说"已经帮你置顶啦"
    # 必须有网可拦——那是本轮的洞① 分支①。
    r"|发布|发表|投稿|提交"
    r"|置顶|取消置顶|隐藏|下架|设为私密|设为公开|设为草稿|设成私密|设成公开|设成草稿"
    r"|新建|创建|建好|打上|加上|去掉)"

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
    # ④ 后台写动词（20260921，管理助手第二轮）：写工具的洞① —— "已经把它设为私密了"
    #    /"刚刚把标签加上了"这两种最自然的编造口吻，①（要"帮你"）、②（动词表是
    #    开合/显示族）、③（要"把/将"紧跟动作词）三支都抓不到。形态沿用②（时间副词
    #    + 距离容忍 + 自带完成态），动词**只放后台写域**：不碰 发布/提交（②支注释里
    #    的老坑——"你已经提交过河灯啦"是转述用户自己的过往动作）。
    r"|(?:已经?|刚刚|方才)[^\n。！？!?；;，,]{0,12}?"
    r"(?:置顶|取消置顶|隐藏|下架|设为私密|设为公开|设为草稿|设成私密|设成公开|设成草稿"
    r"|改成私密|改成公开|改成草稿|新建|创建|建好|打上|加上|去掉)"
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
    # 管理助手报表（20260921）：这四族返回的也是**站内/本机事实**（服务器读数、
    # 服务状态、留言审核、用户聚合），跑过任何一个同样意味着"我有据可依"。
    # 少了它们，报表轮会落进 5d/5f 的"有帧但无内容工具"分支——那分支下回复里
    # 任何「暂无待审」「没有异常」都会被读成洞④（站内结论无帧）而整轮 fallback。
    "get_server_status", "get_service_health",
    "get_moderation_status", "get_user_stats",
    # 后台文章列表（20260921 第二轮）：读它 = 拿到全站文章 id/标题/状态，是
    # "把《X》设为私密"这类**指代**的唯一数据来源（公开列表读不到草稿/私密）。
    # **三个写工具刻意不进这个集合**：本集合的语义是"跑过 ⇒ 检索/读取声称有据"，
    # 塞写工具会让"建了个标签"变成"我检索过"的证据（5d/5f 的判据是内容域帧）。
    "list_admin_notes",
    # 用户自己的数据（20260923）：跑过 = "我手上就是你自己那份收藏/通知"，
    # 与上面四族同一条道理——缺了它们，"你还没有未读通知"这句站内结论就没有帧。
    "list_my_favorites", "get_unread_summary", "list_notifications",
    # 自己的信箱（20260923 批 8）：跑过 = "我手上就是你自己那封信"，同一条道理
    # ——缺了它，"你信箱里没有未读的信"这句站内结论会被读成洞④（无帧结论）。
    "list_my_messages",
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


def _cmd_prefix_hit(text: str) -> str:
    """`_cmd_prefix_directive` 的子句版：返回**指令式**写了命令前缀的那一句。

    与判据同源（同一次遍历、同一对判据），返回的是 `_sentence_of` 切出的整句——
    含前缀的那一句本身就是给人看的证据，比子句更完整（"我这就打开 EFFECT:x"）。"""
    for m in _CMD_PREFIX_RE.finditer(text):
        i = m.start()
        if not (_inside_quote(text, i) or _inside_code_span(text, i)):
            return _sentence_of(text, i)
        if not _CMD_META_RE.search(_sentence_of(text, i)):
            return _sentence_of(text, i)
    return ""
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


def _clip_clause(clause: str, limit: int = 80) -> str:
    """被声称闸否定的那个子句 → trace 事件里的短字段（20260921）。

    为什么必须记（配合 [[gate 声称判据三洞]] 的复盘纪律）：此前 5c/5d/5f 只记
    "本轮执行了哪些工具"，被否掉的那句话没留下——每次调判据都只能凭手感，误杀
    与漏判都无法从 trace 里复盘。截 80 字足够定位（子句本身不长），换行归一防
    单条事件把 trace 撑成多行。"""
    return " ".join(clause.split())[:limit]


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


def _phantom_tool_claim_span(reply: str, executed: set[str], exec_memory: bool,
                             frame_text: str = "") -> tuple[str, str] | None:
    """命中即返回 (工具名, 那个子句)，无命中 → None（判据见上方注释块 + 下方豁免）。

    `frame_text`（本轮所有工具帧的正文拼接）非空时启用**回声豁免**（20260921）：
    该工具名**出现在本轮工具自己的返回文本里** ⇒ narrator 是在复述工具说的话，
    不是在声称自己调用过它。这不是理论上的洞，是生产事故的根：
    165645 管理员建「大笨狗」标签**真的建成了**（id=15），`create_tag` 的返回文本
    尾句自带另一个工具名（「要挂到文章上用 set_article_tags」），narrator 照抄 ⇒
    被判 5c ⇒ 整条回复被换成"这一轮什么都没执行"，与刚发生的执行**当面矛盾**。
    配套硬约束：工具的返回文本**不许再出现任何工具名**（见 adminops.render_tag_created
    与 test_skills.py 的同源 lint）——把这条豁免的适用面压到零。
    """
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
            if frame_text and name in frame_text:
                continue          # 复述本轮工具自己说过的话
            if _tool_claim_window(text, start, end, name):
                return (name, clause)
    return None


def _phantom_tool_claim(reply: str, executed: set[str], exec_memory: bool,
                        frame_text: str = "") -> str | None:
    """薄封装：只要工具名（既有语料/调用点用）。"""
    hit = _phantom_tool_claim_span(reply, executed, exec_memory, frame_text)
    return hit[0] if hit else None


def _clause_hit(text: str, rx, exempt, need_done: bool = False,
                veto=None) -> str | None:
    """子句级判定：返回**第一个**无豁免却命中 rx 的子句；没有则 None。

    返回子句而不是 bool（20260921）：被否掉的那句话要落 trace（见 `_clip_clause`），
    而"判据到底看到的是哪句"只有判据自己知道——让调用方拿正则再跑一遍去猜，猜出来
    的子句未必是同一条（need_done/veto 的口径不同）。判定语义见下，与旧实现一字不差。

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
            return clause
    return None


def _clause_hits(text: str, rx, exempt, need_done: bool = False, veto=None) -> bool:
    """`_clause_hit` 的布尔壳（既有调用方与断言按 bool 写的，语义不变）。"""
    return _clause_hit(text, rx, exempt, need_done, veto) is not None


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


def _state_action_claim_clause(text: str, exec_memory: bool = False) -> str:
    """`_state_action_claim` 的子句版（trace 用，见 `_clause_hit`）。"""
    return _clause_hit(text, _STATE_ACTION_CLAIM_RE, _STATE_ACTION_EXEMPT_RE,
                       need_done=True, veto=_prior_time_veto(exec_memory)) or ""


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


def _chat_tool_claim_clause(text: str) -> str:
    """`_chat_tool_claim` 的子句版（trace 用）。判据同源，只是把命中的那句带回。"""
    for s in _SENT_RE.split(text):
        if not _CLAIM_DONE_RE.search(s):
            continue
        for c in _CLAUSE_RE.finditer(s):
            clause = c.group(0)
            for m in _CHAT_TOOL_CLAIM_RE.finditer(clause):
                if _NEG_BEFORE_RE.search(clause[max(0, m.start() - 6):m.start()]):
                    continue
                return clause
    return ""


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


def _site_search_claim_clause(text: str, exec_memory: bool) -> str | None:
    """命中即返回**那个子句**（trace 用），无命中 → None。

    为什么要把子句带出来（20260921）：`record("gate","phantom_search_claim")` 此前只记
    执行工具集，被否定的那句话没留下——判据调优只能靠"再跑一遍看运气"，误杀复盘无从
    下手（同族问题见 `_phantom_tool_claim_span`）。判据逻辑与 `_site_search_claim`
    逐字相同，后者是它的薄封装。"""
    text = "".join(s + "。" for s in _SENT_RE.split(text) if _STATE_DONE_RE.search(s))
    for c in _CLAUSE_RE.finditer(text):
        clause = c.group(0)
        if not (_SITE_SEARCH_CLAIM_RE.search(clause) or _CHAT_SCAN_CLAIM_RE.search(clause)):
            continue
        if _SEARCH_CLAIM_EXEMPT_RE.search(clause):
            continue
        if exec_memory and _PHANTOM_PRIOR_RE.search(clause):
            continue
        return clause
    return None


def _site_search_claim(text: str, exec_memory: bool) -> bool:
    """站内检索声称（gate 洞②）：站内内容域检索完成式表述。

    _CHAT_SCAN_CLAIM_RE 一并纳入（它的词表是 20260905 事故现场调过的，
    只是词序漏了"站内我查了一圈"形态）。exec_memory=True（本轮带跨轮回执）
    且子句含追述时间词 → 属 rule 6 的据实转述，不判。同 _state_action_claim
    一样要求**同句完成态**（整段话与洞①共用一条判据纪律：完成态才算声称）。"""
    return _site_search_claim_clause(text, exec_memory) is not None


# ── gate 洞④：站内"没有"结论无依据（20260921）────────────────────────────
# 事故形态：访客问一个**通用问题**（"Rust 的 async/await 是怎么工作的？"），planner
# 判"无需检索"（零帧），narrator 答完通用知识又顺手对站内下结论——"站内没有讲这个的
# 文章"。通用知识那半未必错，**站内结论那半没有依据**：本轮没有任何内容类工具跑过。
# 依据 = golden `rag_noise_rust` 7 跑 2 红：形态①零工具答通用知识（连正断言也缺）、
# 形态②零工具却断言「站内没有」（正断言命中、只有帧断言红）——后者是真缺口，故在
# 判据侧补这一洞（用例侧同步把问题改成站点锚定，见该用例 `_note`）。
# 与 5d 互补：5d 抓"我查了一圈"（声称**做过动作**），本判据抓"站内没有"（声称**知道
# 结论**）；两者共用同一事实前提——本轮没跑过内容类工具（_CONTENT_TOOLS）。
# 作用域刻意收窄（宁漏勿误伤，gate fallback 会吞掉整轮回答）：
#   ① 只认**内容域**（文章/内容/教程/说说/留言…）："站内没有下载板块/友链页面"这类
#      **页面/入口**结论由 NAV_MAP 与 SITE_GUIDE 给定，是确定性知识（导航零工具注记轮
#      正靠它如实作答），不判；
#   ② 疑问/条件/提议/转述语境（"要不要我去查查有没有"/"要是站内没有"/"你说站内没有"）
#      不是结论；
#   ③ 依据豁免（比洞①/② 的"回执在场 + 追述时间词"更宽，因为这里说的是**结论**而非
#      追述动作）：跨轮回执里**有检索类动作**（`站内检索「X」`/`搜索「X」`）时，
#      "站内没有"就有系统记录可依（rule 6 据回执转述），放行——只有 8 行窗口里的
#      检索回执才算，更早的检索对模型同样不可见。
#   ④ 跨子句桥（_ABSENCE_LEAD_RE）：同子句形态之外，还认"站内那些文章，没有写过
#      X"这种**逗号断句**——中文里这比同子句形态更常见，漏掉它判据只覆盖一小半
#      真实措辞。桥的两端各设一道闸：本子句必须有站内词 + 内容域名词（页面类结论
#      没有内容域名词，照旧豁免），下一子句必须**以否定存在领起**（≤4 字语气词）
#      且自身不豁免——不做"同句内任意位置搜否定词"，否则"站内文章我读完了，X 也
#      没有报错"会被误判成站内结论。
_SITE_DOMAIN_RE = re.compile(
    r"站内|全站|站里|博客里|博客中|这个站|本站|文章库|你写的|博主写的|所有文章|全部文章")
_ABSENCE_RE = re.compile(
    r"没有|没找到|没写|没讲过|没提|没介绍|未收录|暂无|查不到|找不到|未见|无相关|不涉及")
# 内容域名词（"页面/板块/入口/功能"刻意不收——见 ①）
_CONTENT_NOUN_RE = re.compile(
    r"文章|内容|教程|笔记|资料|文档|博文|写过|讲过|提过|介绍|收录|涉及|说说|留言|帖子")
# 疑问/条件/提议/转述语境。注意**不收「呢」**：人设句尾常用它（"这个站里没有写过
# 相关内容呢"），收进来会把整片真结论漏掉；「吗」保留（明确的疑问语气词）。
_ABSENCE_EXEMPT_RE = re.compile(
    r"要是|如果|假如|假设|除非|若|为什么|是不是|有没有|难道|吗|[?？]"
    r"|要不要|需不需要|可以|能否|能帮|帮你|让我|我来|去查|去搜|查查|搜搜|翻翻|找找"
    r"|你说|你问|你提到|你让我|引用|原话"
    r"|网上|网络|互联网|通用|常识|训练|资料里")
# 跨子句桥用的"否定领起"（中文把结论写成"站内那些文章，没有写过 async 的"这种
# 逗号断句是很常见的形态；只在本子句**开头**出现否定存在时才算，前缀白名单只收
# 副词/语气词——不用"任意 ≤N 字"的窗口，否则"站内文章我读完了，X 也没有报错"
# 会因"X 也"占位而被读成站内结论）
_ABSENCE_LEAD_RE = re.compile(
    r"^(?:(?:确实|真的|其实|目前|现在|暂时|根本|压根)|[也确实都并]){0,2}"
    r"(?:没有|没找到|没写|没讲过|没提|没介绍|未收录|暂无|查不到|找不到|未见"
    r"|无相关|不涉及)")
# 跨轮回执行记忆里的"检索类动作"痕迹（Rust render_exec_row 定稿措辞：rag_search →
# "站内检索「…」"、search_notes → "搜索「…」"）——有它即视为站内结论有据
_EXEC_SEARCH_TRACE_RE = re.compile(r"站内检索「|搜索「")

# ── 确定性收尾轮的洞④ 豁免锚（20260922）─────────────────────────────
# 洞④ 抓的是 narrator **凭空**对站内下"没有"结论。但有两类收尾轮里那句话是**系统自己
# 核对出来的事实**：`_write_target_refusal`（目标预检：站内台账里查无此名/此片段）与
# `_drop_terminal`（点名工具全被剔除 ⇒ 这项数据查不到）。这两轮的注记写明了"请把这条原因
# 如实转告主人"，narrator 复述它就是履职，不是编造——可它偏偏长得跟凭空结论一模一样，
# 于是被整轮换成兜底道歉，而道歉说的还是假话（"我其实没有去站里查过"，可系统确实查过）。
# 实证：20260922 golden `admin_board_unresolved_target_honest` 首跑 resets=1，用户拿到的
# 是道歉而不是"站内没有含「…」的留言"这条真结论。
# 豁免判据 = **计划注记里带这个前缀**（注记是系统产物，narrator 写不进去）。两条收尾路径
# 共用同一份字面量，改一处必须改另一处（test_skills 有锁）。
# 边界（如实说明）：豁免是**整轮**的——收尾轮里 narrator 若另起一个与台账无关的"站内没有"
# 也会被放过（换的是"真结论不再被吞"）；注记里已用禁止句约束它，洞①/②/5c/5d 照旧生效。
_LEDGER_NOTE_PREFIX = "【系统台账核对】"


def _exec_memory_has_search(msgs: list) -> bool:
    """跨轮回执行记忆里是否留下过检索类动作（见 _EXEC_SEARCH_TRACE_RE）。"""
    return any(_EXEC_SEARCH_TRACE_RE.search(str(getattr(m, "content", "")))
               for m in msgs)


def _site_absence_claim_clause(text: str, search_evidence: bool = False) -> str | None:
    """命中即返回**那个子句**（trace 用），无命中 → None（判据见 `_site_absence_claim`）。"""
    if search_evidence:
        return None
    clauses = [c.group(0) for c in _CLAUSE_RE.finditer(text)]
    for i, clause in enumerate(clauses):
        if _ABSENCE_EXEMPT_RE.search(clause):
            continue
        if (_SITE_DOMAIN_RE.search(clause) and _ABSENCE_RE.search(clause)
                and _CONTENT_NOUN_RE.search(clause)):
            return clause
        if (i + 1 < len(clauses) and _SITE_DOMAIN_RE.search(clause)
                and _CONTENT_NOUN_RE.search(clause)
                and _ABSENCE_LEAD_RE.search(clauses[i + 1])
                and not _ABSENCE_EXEMPT_RE.search(clauses[i + 1])):
            # 跨子句桥形态：把**结论那两句**一起交出去（前子句给对象、后子句给否定）
            return clause + clauses[i + 1]
    return None


def _site_absence_claim(text: str, search_evidence: bool = False) -> bool:
    """站内"没有"结论无依据（gate 洞④，见上方注释）。

    两种形态都认（子句级豁免照样生效）：
      ① 同子句三条件：站内空间词 + 否定存在 + 内容域名词（"站内没有讲过这个文章"）；
      ② 跨子句桥：本子句有站内词 + 内容域名词，**下一子句以否定存在领起**
         （"站内那些文章，没有写过 async 的""全站翻过的笔记，没讲过这个"）——
         中文逗号断句的常见形态，缺了它真结论会整片漏掉。
    search_evidence=True（本轮有内容类工具帧，或跨轮回执里有检索痕迹）→ 结论有据，放行。"""
    return _site_absence_claim_clause(text, search_evidence) is not None


# ── 确认话术声称（gate 洞⑥，20260923）──────────────────────────────────────
# "点「确定」我就去办"这类**确认动作声称**：说话人声称系统正等他点确认框。
# 这条判据的依据是**结构**而不是概率：真弹了确认框的那一轮**到不了 gate**——
# `route_after_execute` 见到 `pending_confirm` 直接 END，回复文本由 execute 侧
# 确定性给出（`confirm_text`，见该函数与 `_confirm_popup` 的注释）。
# ⇒ gate 视野里出现"点「确定」我就去办"，**必然是 narrator 自己编的**。
#
# 为什么非要有硬判据（纪律 18 已经逐字写了这条，却还是发生了）：
#   纪律 18 原文："确认框一个字都不要提：真弹了确认框的那一轮根本轮不到你说话…
#   说'已经发起/已提交/请留意确认弹窗/等你点确认'就是编的"。
#   20260923 13:19 实测：主人说"小猫咪按你想法来吧"，planner 因目标解不出被身份
#   防线拦下（trace `write_target_unresolved`），系统**已经**把如实话术交给
#   narrator（`_wrap_up_plan` 的 note："这件事这次没有做…不许出现看过/读过/查过"），
#   narrator 却回了"我把这条驳回隐藏…点「确定」我就去办"，gate PASS。
# ⇒ prompt 层纪律不是防线，判据才是。
#
# 全量真实 trace 复扫（238 条，20260923）：真弹窗轮 65 条（按 `consent_popup`
# 事件识别，全部不经过 gate）；非弹窗轮**正则命中 4 条**（其中 1 条被下面那张
# 豁免表以将来时放行 ⇒ **真判 3 条**，即误伤 0），其中
#   · 1 条即上面那条（目标还错成了模型历史里的旧留言）；
#   · 2 条更重（`20260922T003512` / `20260922T193001`，同一天两跑）：「把文章 1
#     改成草稿」**其实已经执行并复核通过**（execute call + checker PASS +
#     「已修改文章 1…私密 → 草稿」），回复却说"我先跟你确认一下…点「确定」我就去办"
#     ——把办好的说成待确认（抄的正是上一轮 `recent_tail` 里系统的确认文本，
#     与"写给 narrator 的机制描述会变成它的词汇"同一条教训）；
#   · 1 条是**将来时描述**（`20260922T054528`："你发给我之后…系统会走确认流程——
#     点「确定」我就去办"），合法 ⇒ 豁免表收将来标记（会/将/之后…），见下。
_CONFIRM_CLAIM_RE = re.compile(
    # ① 承诺形态：点「确定」我就去办 / 点了确定就执行 / 等你点确认
    r"点\s*[「\"'『]?\s*(?:确定|确认)[」\"'』]?\s*(?:我|就|即|便|后)"
    r"|(?:等|等候|等待)\s*(?:主人|你|您|访客)?\s*(?:去)?\s*(?:点|按|戳)\s*[「\"'『]?\s*(?:确定|确认)"
    # ② 完成/进行形态：确认框已经弹出来了 / 我已发起确认 / 确认弹窗在等你
    r"|(?:确认框|确认弹窗|弹窗)\s*(?:已经|已|就|正)?\s*(?:弹|出现|显示|在等|等着|挂)"
    r"|(?:已经|已|我)\s*(?:经)?\s*(?:发起|走了|推送|提交)\s*了?\s*确认",
    re.S)
# 豁免（同子句内生效，见 `_clause_hit`）：将来时描述（"系统会走确认流程——点确定我就
# 去办"）、否定（"没有弹确认框"多数形态因语序本就命中不了，这里再兜一层）、
# 转述（"你说点确定""你说的'等你点确认'"）。
_CONFIRM_EXEMPT_RE = re.compile(
    r"(?:会|将|之后|届时|到时候|未来|下次)"      # 将来时 ⇒ 说的是"到时候会弹"，不是"现在正等着"
    r"|(?:没有?|未|不会|别|不必|不用)\s*(?:弹|发|等|点)"
    r"|(?:你|主人|他|她|访客)\s*(?:说|问|提到|指的是|那句)"
    # 转述他人/站内内容的词（留言、说说、公告、访客原话里都可能出现"点确定"这种字面）
    r"|(?:写|标|照抄|引述|转述|转告|复述)(?:着|的是|的|了)?"
    r"|(?:留言|评论|说说|公告|原话|正文|内容是)"
    r"|(?:如果|要是|倘若|假设)", re.S)


def _confirm_claim_clause(text: str) -> str:
    """确认话术声称的子句（trace 用，见 `_clause_hit`）；没有则空串。

    ⚠️ **不剥引号**（与 `_claim_issue` 里其它判据的做法相反）：系统的确认文案本身
    就是"点「确定」我就去办"——「确定」两个字**永远**在引号里，剥掉引号等于把这条
    判据的存在意义剥没了（写完当场实测：四条真话全判空）。转述豁免改由豁免表里的
    "写着/留言/原话"那族承担。"""
    return _clause_hit(text, _CONFIRM_CLAIM_RE, _CONFIRM_EXEMPT_RE) or ""


def _confirm_claim(text: str) -> bool:
    """确认话术声称（gate 洞⑥）：本轮没弹确认框却说"点「确定」我就去办"。"""
    return bool(_confirm_claim_clause(text))


# NOTE 零工具（页面不存在/已下线）轮的如实措辞核验词表（与 instantiate_plan 的
# note 文本配套，见 gate_node）。
_HONEST_DOWN = ("下线", "下架", "无法访问", "没有了")
_HONEST_GONE = ("没有", "不存在", "找不到", "无法识别", "没有找到")


def _claim_issue(reply: str, skill: str, plan: dict, frames_exist: bool,
                 exec_memory: bool = False,
                 exec_search_evidence: bool = False,
                 has_popup: bool = False) -> tuple[str, str, str] | None:
    """声称闸判定（gate 确定性兜底，20260902 事故族）：回复含声称但轨迹无工具
    支撑 → 返回 (issue, 人设内 fallback 文本, **被否掉的那一句**)；有据/无声称 → None。

    第三项是给 trace 的（20260921）：误杀复盘此前只能看到"判了哪一族"，看不到
    "判的是哪句话"——而每一次调判据争论的恰恰是那句原话。空串 = 判据没有具体
    句子可指（如帧存在直接放行，本来也没进这里）。

    作用域（20260903 收窄后的设计 + 20260919 两洞 + 20260920 洞③）：
      - 任何轮：命令前缀文本（_cmd_prefix_directive——引号/内联代码区 + 同句机制词
        = 元讨论里的提及，放行；见该函数注释与 golden `forbid_fallback`）
      - 任何轮：确认话术声称（_confirm_claim，洞⑥，20260923）——"点「确定」我就去办"
        这类声称与帧无关（有帧轮也可能是假的：写完了却报成待确认），依据是**结构**：
        真弹窗轮由 `route_after_execute` 直接 END、到不了 gate（见该正则上方长注）
      - 零工具轮（不分技能）：操作完成声称（_STATE_ACTION_CLAIM_RE，洞①）与
        站内检索声称（_site_search_claim，洞②）——零帧 = 本轮什么都没发生，
        这两族声称必为编造。**两族共用同一条回执豁免**（20260921 补齐）：本轮带跨轮
        执行回执（executions 注入）且子句含追述时间词 → 说的是**已记录的那次执行**，
        属 rule 6 据实转述；此前只有洞② 接了 exec_memory，洞① 漏了，导致
        "刚才已经帮你显示上去了"这类**引回执**的回合被整轮换成兜底道歉
      - 零工具轮（除两类收尾轮）：站内"没有"结论无依据（_site_absence_claim，
        洞④，20260921）——"站内没有讲这个的文章"这类**结论**同样要本轮查过才有资格说；
        依据豁免比洞①/② 宽（跨轮回执里有检索痕迹即放行），因为这里说的是结论不是动作。
        收尾轮豁免两类（20260922 补第二类）：navigate 注记轮（NAV_MAP 的确定性事实）与
        带 `_LEDGER_NOTE_PREFIX` 的确定性收尾轮（站内台账的核对结果，见该常量长注）
      - chat 零工具轮：另查第一人称工具调用声称（_CHAT_TOOL_CLAIM_RE）——
        高精确模式；"重读/查过"读取声称不在此拦（chat 轮多为口语，误伤成本高）
      - content_query 零工具轮（异常路径：计划本应有调用清单却留空收尾）：
        三族全查（读取/执行/调用声称）——该场景"本该查证"，声称误伤成本低
      - 有帧轮：读取/调用声称天然有据，不做文本对照；只兜 err 帧 + 完成式
        声称、NAVIGATE: 确认帧 + 到达声称、具名工具声称（5c）、**站内检索声称
        与内容类帧族不符**（5d，洞②的混合轮形态）——见 gate_node
    """
    hit = _cmd_prefix_hit(reply)
    if hit:
        return ("cmd_prefix", _FALLBACK_CMD_PREFIX, hit)
    # 洞⑥（20260923）：确认话术声称——**任何轮次都查**，包括有帧轮。
    # 位置在 `if frames_exist: return None` **之前**是刻意的：这一条说的不是"有没有
    # 干活"，而是"有没有在等主人点确定"，与帧无关（20260922 那两条正是**有帧**的轮
    # ——写已经执行并复核通过，回复却说在等确认）。has_popup=True 只作防御性豁免：
    # 弹窗轮本该到不了这里（route_after_execute 见 pending_confirm 直接 END）。
    if not has_popup:
        span = _confirm_claim_clause(reply)
        if span:
            return ("confirm_claim_without_popup", _FALLBACK_CONFIRM_CLAIM, span)
    if frames_exist:
        return None  # 帧存在：声称有据（err 帧/确认帧/具名/检索族场景由 gate_node 兜）
    # 引号内是被转述的访客留言/说说正文，不算 narrator 自己的声称（20260913：
    # 留言板里那句"执行调用 navigate_to"被转述时误伤）
    own = _strip_quoted_spans(reply)
    if _state_action_claim(own, exec_memory):
        return ("state_claim_without_tool", _FALLBACK_STATE_CLAIM,
                _state_action_claim_clause(own, exec_memory))
    if _site_search_claim(own, exec_memory):
        return ("search_claim_without_tool", _FALLBACK_SEARCH_CLAIM,
                _site_search_claim_clause(own, exec_memory) or "")
    # 洞④（20260921）：站内"没有"结论无依据。两类收尾轮豁免——它们注记里的那句话是
    # **系统给的确定性事实**，narrator 的职责就是如实转告，不属凭空结论：
    #   ① navigate 的零工具注记轮："页面不存在/已下线"来自 NAV_MAP
    #      （gate_node 第 4 节另有如实措辞核验）；
    #   ② 确定性收尾轮（`_LEDGER_NOTE_PREFIX`，20260922）：目标预检/剔空收尾给的是
    #      站内台账的核对结果（"站内没有含「…」的留言"）——见该常量的长注。
    _note = plan.get("note") or ""
    if not (skill == "navigate" and "不调用任何工具" in _note) \
            and _LEDGER_NOTE_PREFIX not in _note:
        if _site_absence_claim(own, exec_search_evidence):
            return ("site_absence_claim_without_tool", _FALLBACK_SITE_ABSENCE,
                    _site_absence_claim_clause(own, exec_search_evidence) or "")
    if skill == "chat":
        if _chat_tool_claim(own):
            return ("claim_without_tool", _FALLBACK_CLAIM, _chat_tool_claim_clause(own))
        return None
    if skill == "content_query":
        for rx in (_READ_CLAIM_RE, _EXECUTION_CLAIM_RE, _CALLED_TOOL_CLAIM_RE):
            m = rx.search(own)
            if m:
                return ("claim_without_tool", _FALLBACK_CLAIM,
                        _claim_clause(own, rx) or m.group(0))
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
# 有帧轮的两个变体（20260921）。上面两条文案都断言行"系统没有任何工具执行"，
# 而它们在 5c/5d 上**每一次命中都与回执矛盾**：5c 的 `_phantom_tool_claim` 在
# `not executed` 时直接返回 None（只在真有执行的轮才可能命中）、5d/5f 整段位于
# gate_node 的"有帧轮"分支（`if not frames: return` 之后）——两处都必然有帧。
# 生产实证 165645：`create_tag` PASS（「大笨狗」id=15 真建成了），narrator 照抄
# 工具返回文本里的另一个工具名被判 5c，回复被替换成"这一轮什么都没执行"——
# 与执行回执、与用户刚看到的结果**当面矛盾**。文案只许否认**被点名的那件事**。
_FALLBACK_PHANTOM_CLAIM = (
    "喵呜……主人，我得纠正自己一句：我刚才说某个工具是我调用的，可这一轮系统记录里"
    "**没有那次调用**——我嘴上多说了。这一轮真正执行过的是别的事，我说了什么、系统"
    "做了什么，一律以系统记录为准。你要的那件事，要我现在真的去做一遍嘛？")
_FALLBACK_SEARCH_CLAIM_FRAMED = (
    "喵呜……主人，我得说实话：这一轮我确实动手做了些事，但**站内的内容我一条都没"
    "查过**——我说的『翻了一遍/检索了一圈』是嘴上跑火车。要不要我现在认认真真查"
    "一遍再回答你？这次每一条都带真实来源喵。")
_FALLBACK_SITE_ABSENCE = (
    "喵呜……主人，我得收回一句：这一轮我其实**没有去站里查过**，却说成了『站内没有"
    "…』——站里到底有没有，我没核实过就不能下结论 :犯错: 要我现在认认真真检索一遍"
    "再回答你嘛？这次查到什么、没查到什么都如实告诉你喵。")
# 洞⑥（20260923）：本轮没弹确认框，却说了"点「确定」我就去办"这类确认话术。
# 文案必须对**两种实况都成立**（判据不区分，因为判据看到的是同一句假话）：
#   ① 什么都没做（13:19 那条：写被身份防线拦下、零帧）；
#   ② **其实已经做完了**（20260922 那两条：写已执行并复核通过，回复却说"等确认"）。
# 所以不写"这件事没办"，只写"没有确认框在等你 + 状态以系统记录为准"。
_FALLBACK_CONFIRM_CLAIM = (
    "喵呜……主人，我得纠正自己一句：这一轮系统**没有弹任何确认框**，也没有在等谁点"
    "「确定」——我刚才那句『点「确定」我就去办』是句空话，系统那边根本没有这个待确认"
    "的动作 :犯错: 这件事现在到底是还没做、还是已经做完了，一律以系统记录为准，别信"
    "我上一条的措辞。要不要我重新走一遍？（该确认的会真的弹窗给你）")
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
# 写操作被同意闸拦下（20260921 §5.2 缺口③）：这条**不是"系统失败"**——系统好好的，
# 只是还没得到主人的点头。此前一律套 _FALLBACK_ERR_CLAIM（"操作系统返回的是失败
# （执行出错了）"），与事实不符且把用户引向"再试一次"这种无效路径。
_FALLBACK_CONSENT = (
    "喵呜……主人，这件事我**还没有动手**——它会改动站上的数据，我在等你的明确"
    "同意。你说一句「确认」、或者直接说「把…（具体怎么做）」我就照办；不想改的话"
    "忽略这句就好，站上什么都没变 :害羞:")
# 目标无据（20260921 第二轮）：不知道改哪一篇，同样不是"失败"。
_FALLBACK_UNKNOWN_TARGET = (
    "喵呜……主人，我**还没有动那篇文章**——我不确定你说的是哪一篇，不敢凭印象"
    "填一个编号（改错了是要紧事）。你告诉我文章名字或编号，或者让我先把后台文章"
    "列表读出来给你看，我再动手喵。")
_FALLBACK_DOWN = (
    "喵呜……那个板块确实已经下线了，刚才说得好像还能去一样，是我不好。现在站里"
    "能逛的真实页面是：首页、留言板、说说、时间轴、关于我～要去哪边嘛？")
_FALLBACK_GONE = (
    "喵呜……主人，那个页面我在站里确认过是不存在的，刚才不该说得像真的一样。"
    "站里真实能去的页面有：首页、留言板、说说、时间轴、关于我、登录、管理后台、"
    "物联网平台。要不要我带你逛逛？")


def _claim_clause(text: str, *rxs) -> str:
    """第一个命中任一 rx 的**子句**（trace 里"被否掉的那句话"，20260921）。

    与 `_clause_hits` 用同一套切分（`_SENT_RE` / `_CLAUSE_RE`）：trace 里的子句
    必须与判据看到的子句同粒度，否则复盘时"判据到底看到的是哪句"又得靠猜。
    找不到 → ""（调用方据此决定要不要写这个字段）。
    """
    for s in _SENT_RE.split(text or ""):
        for c in _CLAUSE_RE.finditer(s):
            clause = c.group(0)
            for rx in rxs:
                if rx.search(clause):
                    return clause
    return ""


def _fallback_result(issue: str, text: str, plan: dict, frames: int,
                     clause: str = "") -> dict:
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
    record("gate", "fallback", issue=issue, skill=plan["skill"], frames=frames,
           **({"clause": _clip_clause(clause)} if clause else {}))
    logger.info("[gate] fallback（%s）: skill=%s frames=%d%s", issue, plan["skill"], frames,
                f" clause={_clip_clause(clause)}" if clause else "")
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

    # 确认轮（20260921）：用户在确认框上点了确定——**零 LLM 直接照令牌拼计划**。
    # 这是"隐藏确认请求"这条通道的全部意义：不花一次 planner 决策，也不给模型
    # "重新理解一遍用户想要什么"的机会（它只该执行签名里那件事，一个字都不许改）。
    #
    # **只在首轮（rounds==0）走这条**：确认轮的执行若受阻，控制权会回到这里
    # （route_after_execute 只在"无受阻"时直去 model）。那时若再照令牌拼一次
    # 同一份清单，就是把同一件写操作**做第二遍**——所以第二轮一律转确定性收尾，
    # 由 narrator 拿着真实回执如实说结果（这与"宁可少做也不做错"的写侧纪律一致：
    # 令牌只授权一次执行，不是一张可反复使用的通行证）。
    grant = state.get("confirm_grant")
    rounds = state.get("plan_rounds", 0)
    if grant:
        if rounds == 0:
            plan_obj = _confirm_grant_plan(grant)
            record("planner", "confirm_grant", skill=plan_obj["skill"], tools=plan_obj["tools"])
        else:
            plan_obj = _wrap_up_plan(_has_frames(state["messages"]))
            record("planner", "confirm_wrap", rounds=rounds,
                   reason="确认轮执行受阻，不重发清单")
        return {"plan": plan_encode(plan_obj), "plan_rounds": rounds + 1, "done": False}

    user_msg = _last_user_msg(state["messages"])
    # 角色要在**取 page_ctx 之前**定：能力清单按角色渲染（20260921——清单里不含
    # 管理能力是 narrator 讲"我不能改后台"的"依据"，见 context.site_guide）。
    role = _principal_of(config).known_role
    page_ctx = _page_ctx(state["messages"], role)
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
    # 工具帧文本先算一次（下面 format 里要用，trace 里也要记长度）——20260920 起
    # 落 `frames_chars`：单帧上限 20000 是拍出来的经验值，没有真实体量数据就无法
    # 判断"该收该放"（超长文章改造后尤其要能看见节选是否生效）。
    frames_txt = _frame_texts(state["messages"])

    # ── 决策（最多两次：正常一次 + 剔空纠偏一次）─────────────────────────
    # 20260921 22:34 生产实证（用户报："被降级了但是居然就直接结束而不是重新规划
    # 执行"）：管理员问「小猫咪那篇文章都有什么标签呀」，planner 点名
    # list_admin_notes——**意图是对的**（那篇是草稿，公开接口看不见，只有后台工具
    # 读得到），但 content_query 的 calls 白名单里没有它（它属于 admin_notes **技能**）
    # ⇒ 清单被剔空 ⇒ 旧行为把"剔空"当成"无需工具的收尾轮"（route_after_planner 见
    # TOOLS 空即去 model）⇒ narrator 对着零工具零帧编出「我刚才查看了文章列表和读取了
    # 文章详情」⇒ gate 打回 ⇒ 用户只看到一句"被抓包"的降级回复，**本轮就此结束**。
    # 剔空不是"不用查"，是"点错了通道"：确定性纠偏一次——把"你点名的工具一个都没执行"
    # 与"它属于哪个技能/为什么够不到"（机器从注册表读的）写给它看，让它重新决策
    # （planner 仍是唯一决策者，这里不替它选技能）。两次都剔空 → 确定性如实收尾。
    correction = ""
    for _attempt in (0, 1):
        _t0 = time.monotonic()
        logger.info("[planner] LLM 调用开始（round %d/%d%s）", rounds + 1, MAX_PLAN_ROUNDS,
                    "，剔空纠偏" if correction else "")
        try:
            _prompt = _PLANNER_PROMPT.format(
                # 技能表按本轮角色过滤（20260921）：管理助手那三个技能只对 admin 列出，
                # 其余角色看不到 ⇒ 选不出来。用 known_role（未知角色 → None → 只列公开技能）
                skills_context=build_planner_context(role),
                tools_desc=_QUERY_TOOLS_DESC,
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
                correction=correction or "（本决策轮无纠偏提示）",
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
               frames_chars=len(frames_txt), corrected=bool(correction),
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

        # 写操作的目标按名字解不出来 → 不弹窗、不执行，直接确定性如实收尾
        # （见 _write_target_refusal 上方长注：名字通道下"解不出来"必须响亮，
        # 而"响亮"的最省事形态就是**根本不问那一句**）。
        # 先过片段地基（20260922 ②防线）：留言的 quote 校正到主人引号里那段原话
        # （或在没有可指认的片段时确定性拒绝）——**必须在目标预检之前**，否则预检
        # 判的是 planner 那个被截短/被概括错的片段。
        quote_refuse = _board_quote_fix(plan_obj, user_msg, rounds)
        # 公告的 title/content 同样有"主人自己标出来的原话"通道（20260922 ②防线续）：
        # 没有可拒绝的形态（公告一律弹窗、主人签字前看得见），只做校正。
        _announcement_text_fix(plan_obj, user_msg)
        # 标签/分类/公告的**目标名**同理（②防线续二）：引号里那一段就是主人点名的
        # 那一个，planner 抄短了就校正回来——**必须在目标预检之前**，否则预检报的是
        # 另一个名字（"站内没有叫「绝对」的标签"）。
        _name_target_fix(plan_obj, user_msg)
        # 写参数里的**名字值**（新名字 / 标签名列表 / 父标签）同理（②防线续五，见
        # `_name_arg_fix` 上方长注）：新建的名字天然不在字典里，只能来自主人这句话。
        value_refuse = _name_arg_fix(plan_obj, user_msg)
        subject = "站内的台账（标签/分类字典、公告清单、留言列表）与主人这句话本身"
        refusal = None
        if quote_refuse:
            refusal = (_tool_name((plan_obj.get("tools") or ["?"])[0]), quote_refuse)
        elif value_refuse:
            refusal = value_refuse
            subject = "主人这句话本身（要写进站内的名字只能来自这里）"
        else:
            refusal = _write_target_refusal(plan_obj, config)
        if refusal:
            wtool, why = refusal
            # 值被拒时补一句：那个字面是**系统自己的参数值**，不是主人点名的名字
            # （20260922 探针 ⑤ 实测：如实答复里出现了"站内并没有叫「音乐」的现成
            # 标签"——系统查的是占位文字「标签名」，叙述把两者画了等号 = 假话）。
            value_tail = ("" if not value_refuse else
                          "系统要填进参数的那个字面是**系统自己的参数值**，"
                          "不是主人点名的名字——转述它时**原样引述**，"
                          "绝不许把它说成主人说的那个名字。")
            logger.warning("[planner] 写操作参数解不出「主人这句话」里的来源（%s）：%s"
                           " → 确定性如实收尾", wtool, why)
            record("planner", "write_target_unresolved", tool=wtool,
                   reason=why[:160], round=rounds)
            plan_obj = _wrap_up_plan(False, note=(
                _LEDGER_NOTE_PREFIX +
                "**这件事这次没有做：站内数据一个字节都没有改动**"
                "（本轮一个工具都没有执行）。"
                f"系统核对过{subject}，结果是：{why}。"
                "请把这条原因**如实**转告主人（连同里面的候选名单或该补的信息），"
                "并问他接下来想怎么办（换个说法、或先把那个目标建出来）。"
                "**不许**出现「看过/读过/查过/检索过/调用过工具」这类说法；"
                "也**不许**把它讲成一篇内容层面的结论。"
                + value_tail))
            # ⚠️ 这里必须是 **return**，不是 break：决策循环之后的收尾路径会读
            # `plan_obj["params"]`（只有 instantiate_plan 的产物才有这个键），
            # 而 `_wrap_up_plan` 不带它 ⇒ break 到那里必抛 KeyError('params')
            # （20260922 实测：正是本函数要修的那条用例把整轮打成 __ERROR__，
            # 与 20260921 22:37 的 KeyError('model') 同一类错——"分支走通了、
            # 收尾路径没走通"，故 test_skills 里也补了假 LLM 整轮锁）。
            return {"plan": plan_encode(plan_obj), "plan_rounds": rounds + 1,
                    "done": False}

        # 写形态的请求上一条工具规格都没写（见 _name_write_nudge 上方长注）：与
        # 剔空纠偏共用同一条重决策通道（同一轮内只纠一次，纠完仍零工具就照原样走）。
        nudge = None if correction else _name_write_nudge(plan_obj, user_msg, rounds, role)
        if nudge:
            logger.warning("[planner] 主人点名了目标却零工具 → 写形态纠偏重决策：%s",
                           "、".join(_msg_quote_spans(user_msg)[:3]))
            record("planner", "name_nudge", round=rounds,
                   spans=_msg_quote_spans(user_msg)[:3])
            correction = nudge
            continue

        # 剔空纠偏（见上方长注）：只有"点名的全被剔除、本轮一个工具都不剩"才重决策；
        # 已经纠偏过一次、或清单非空、或根本没点名 → 到此为止。
        if correction or plan_obj["tools"] or not plan_obj.get("dropped"):
            break
        correction = _drop_correction(plan_obj["dropped"], role)
        record("planner", "drop_correct", dropped=plan_obj["dropped"], round=rounds)
        logger.warning("[planner] 点名工具全被剔除（本轮零工具）→ 剔空纠偏重决策：%s",
                       "、".join(plan_obj["dropped"]))

    if plan_obj.get("dropped") and not plan_obj["tools"]:
        # 纠偏之后仍然剔空：这一轮**确实什么都查不了**。确定性如实收尾——绝不把
        # "零工具零帧"直接交给 narrator（那正是 22:34 那一轮的形态：它只能编）。
        # 注记里既要写"本轮零执行"这个事实，也要写"你不许说什么"——20260921 的
        # 教训：写给 narrator 的机制描述会变成它的词汇（写"系统会先弹确认框"，
        # 它就照抄成"请留意确认弹窗"），所以纪律要写成禁止句。
        logger.warning("[planner] 剔空纠偏后仍零工具（%s）→ 确定性如实收尾",
                       "、".join(plan_obj["dropped"]))
        record("planner", "drop_terminal", dropped=plan_obj["dropped"], round=rounds)
        plan_obj = _wrap_up_plan(False, note=(
            _LEDGER_NOTE_PREFIX +
            "**本轮一个工具都没有执行**（你点名的那几个工具都在可调用清单之外），"
            "所以你现在**没有任何工具返回可用**。只许如实说明你查不到这项数据："
            "说清缺的是什么（需要用户指明是哪一篇/需要博主身份/站内没有这项数据），"
            "并请用户补充信息。**不许**出现「看过/读过/查过/检索过/调用过工具」"
            "这类说法，也不许描述你做了哪些步骤。"))
        return {"plan": plan_encode(plan_obj), "plan_rounds": rounds + 1, "done": False}

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

    # 快照型报表技能重复规划防护（20260921，与上一条同源、判据不同）：本轮
    # 已 **checker PASS** 过的报表工具再规划一遍，拿回的是同一份快照 —— 直接收尾。
    # 判据取 receipts（系统验收过的事实）而非"帧里出现过工具名"：失败/unavailable
    # 的帧不进回执，planner 按规则 5 改参重试的路径不受影响（重试合法，重取不算）。
    if has_frames and plan_obj["tools"] and plan_obj["skill"] in SNAPSHOT_SKILLS:
        passed = {r.get("tool") for r in (state.get("receipts") or [])}
        planned = {_tool_name(s) for s in plan_obj["tools"]}
        if planned and planned <= passed:
            logger.info("[planner] 报表已取回（%s），去重收尾",
                        "、".join(sorted(planned)))
            plan_obj = _wrap_up_plan(
                True, "本轮已取回的报表数据就在上方工具返回里（快照型只读，"
                      "重复调用拿回同一份数据），基于已有返回如实作答")
            return {"plan": plan_encode(plan_obj), "plan_rounds": rounds + 1,
                    "done": False}

    # 后台写技能重复规划防护（20260921 第二轮，与上一条同源、判据**更严**）：
    # 报表是快照型只读——同工具重复 ⇒ 拿回同一份数据；**写不是**。同一工具名第二次
    # 调用完全可能是"另一篇"或"改成另一个值"（"再帮我把那篇也置顶"/来回切换），
    # 只比工具名的判据会把第二件事静默收尾，而 narrator 手里握着第一条真回执，
    # 必然说成"都改好了"。⇒ 判据 = **(工具名, 参数) 整体**：一模一样的写才收尾
    # （同一件事重复规划），换了参数就是新的事情，放行。
    # 判据同样取 receipts（checker PASS 过的事实）：失败/未确认/目标无据的写不进
    # 回执 ⇒ planner 按规则 5 改参重试、以及"先读再写"的第二次尝试都不受影响。
    if has_frames and _already_done_writes(plan_obj, state.get("receipts")):
            logger.info("[planner] 写操作已执行（%s），去重收尾",
                        "、".join(sorted(_tool_name(s) for s in plan_obj["tools"])))
            plan_obj = _wrap_up_plan(
                True, "这一批后台写操作**已经执行并复核过**，回执就在上方工具返回里："
                      "照它如实报告改的是哪一篇、从什么变成什么。"
                      "**不要**再说「正在改」，被问到时也不许否认；"
                      "若还有没改的，说清楚哪一件没做。")
            return {"plan": plan_encode(plan_obj), "plan_rounds": rounds + 1,
                    "done": False}

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


def _spec_signature(name: str, args: dict) -> tuple[str, str]:
    """(工具名, 参数指纹)——写操作的重复规划判据（比"只比工具名"严一档）。

    指纹按**回执侧的形态**归一：回执构造时把每个值 `str(v)[:200]`（见 execute 的
    rcpt），所以计划侧必须走同一归一化，否则 `{"isTop": 1}` 与回执里的
    `{"isTop": "1"}` 永不相等——判据失效成"从不收尾"（比误收尾更难发现：表现为
    多跑一轮，看不出是判据坏了）。键序用 sort_keys 抹平（取决于 spec 的书写顺序）。
    """
    key = json.dumps({str(k): str(v)[:200] for k, v in (args or {}).items()},
                     sort_keys=True, ensure_ascii=False)
    return (name, key)


def _already_done_writes(plan_obj: dict, receipts) -> bool:
    """这批写计划是否**与已 PASS 的回执逐字相同**（planner 收尾判据，纯函数）。

    收尾的正当性只来自"同一件事已经做过"：`(工具名, 参数)` 整体出现在回执里
    （回执 = checker 验收过的事实），计划里每一件都已如此 ⇒ 这次规划是重复规划。
    换一个参数（"再帮我把那篇也置顶"）就是另一件事，**不由这里收尾**——
    这正是本轮把判据从"比工具名"改成"比 (工具名, 参数)"的原因：同名不同参会被
    只比名字的旧判据静默吞掉，而 narrator 手里握着第一条真回执，必然说成"都改好了"。

    空计划返回 False（没有要执行的，收尾与否不归这条判据管）。
    """
    if not (plan_obj.get("tools") and plan_obj.get("skill") in EXECUTED_ONCE_SKILLS):
        return False
    passed = {_spec_signature(r.get("tool"), r.get("args") or {})
              for r in (receipts or [])}
    planned = {_spec_signature(_tool_name(s), _tool_args(s)[0] or {})
               for s in plan_obj["tools"]}
    return bool(planned) and planned <= passed


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
    if kind == "not_found":
        # 目标不存在 / 不在我能确认的范围内（20260923 三轮，见 tools.base.not_found）：
        # 同样 BLOCK（什么都没改成事实），但与 unavailable 分开给原因码——planner 的
        # 应对是**换个 id 或如实问主人**，不是"稍后再试"；过程行也从「服务不可用」
        # 改成「目标不存在」（server._REASON_CN）。
        return _VERDICT_BLOCK, "target_not_found"
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
                                or authz.consent_error_reason(text)
                                or A.target_error_reason(text) or "error_frame")
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


def _confirm_grant_plan(grant: dict) -> dict:
    """已验签的令牌 → 计划对象（**确定性拼装，零 LLM**）。

    技能名与工具名都取自签名体：`SKILL=` 用令牌里的技能名（**不猜**）、TOOLS 行
    逐条落签名参数、NOTE 写明"用户已确认，不得增改参数"。技能与工具对不上一律
    拒绝（空清单 + 注记）——令牌被换成别的工具（哪怕签名有效也不该发生）是最后
    一道形状检查：execute 拿空清单就什么都不执行，planner 下一轮会看到"没做事"。
    """
    skill_name = str(grant.get("skill") or "")
    specs = [s for s in (grant.get("specs") or []) if isinstance(s, dict)]
    skill = SKILL_MAP.get(skill_name)
    tools = [f"{s.get('tool')}({json.dumps(s.get('args') or {}, ensure_ascii=False)})"
             for s in specs if s.get("tool")]
    # 技能与工具必须**对得上**：令牌里点名了技能 X，清单里的工具就必须是 X 的
    # 固定序列里的（`Skill.plan` 就是那份清单）。这不是防伪造（令牌是我们自己签的），
    # 是防**内部不一致**——签的时候用技能 A、执行的时候却被塞进工具 B，只会是
    # 某处逻辑写错了；而"写操作跑在一份没有人预期它会跑的技能名下"正是最难查的
    # 那类事故。对不上就一个工具都不执行（空清单 + 如实告知）。
    allowed = {t for t, _ in (skill.plan if skill else [])}
    bad = [str(s.get("tool")) for s in specs if str(s.get("tool")) not in allowed]
    if not tools or skill is None or bad:
        note = ("确认令牌里的技能/工具对不上（未执行任何操作）：如实告知主人这次确认无效，"
                "请他说一遍要做什么")
        tools = []
    else:
        note = "用户已在确认框上点过「确定」，照签名参数执行；不得增删改任何参数、不得换工具"
    skill = skill or SKILL_MAP["chat"]
    return {"skill": skill.name, "tools": tools, "note": note,
            "reply": skill.reply_contract, "chat": skill.chat, "dropped": []}


# ── 写操作的目标按名字解不出来（20260922，探针腿⑮）─────────────────────────
# 名字通道把"目标"交给工具在 execute 阶段解析，而**弹确认框发生在执行之前**——
# 于是出现这种形态：系统明知道这个名字在字典里根本没有（或者有歧义），还是问了
# 一句"要不要做"；用户点确定，只能拿到一句"站内没有这个标签、本次未改动"。
# 那等于把一次**信息性回答**包装成了一次**待确认的操作**（探针腿⑮ 实测：
# 「把标签「绝对不存在的标签名xyz」挪到「编程」下面」→ 卡片诚实、零写安全，
# 但用户白点一次）。
#
# 这一层在**规划轮**就把判断做掉：字典读得到、而名字落不到唯一一行时，不弹窗、
# 不调用工具，直接确定性如实收尾（`_wrap_up_plan`：零工具、带注记 → 路由直奔
# narrator 叙述，见 route_after_planner）。
#
# 三条边界（都朝"宁可多问一次，也不误拦一次"的方向）：
#   · **字典读不到（None）不是"没有"**——一律不拦，保持既有行为（弹窗与工具侧
#     各自的"读不到"说法都还在；把一次网络故障变成一句"站内没有"是最坏的错法）。
#   · 参数里还挂着 `$ref` 的 spec 一律不拦：那是"取值没解析出来"，execute 的
#     `resolve_args` 会给带原因码的错误帧，planner 还有机会改参数——与"名字不
#     存在"是两回事。
#   · 只认**写工具的目标字段**；`new_title`/`color` 这些"要改成什么"不参与判断
#     （新建的名字当然不在字典里，那不是错误）。
_WRITE_NAME_FIELDS = {
    # 工具名 -> (目标名字字段, 父标签名字字段)
    "create_tag": (None, "parent_tag"),
    "update_tag": ("name", "parent_tag"),
    "delete_tag": ("name", None),
    "update_category": ("name", None),
    "delete_category": ("name", None),
    # 公告（20260922 第五轮）：按标题指认。新建不在此列——那是**新**标题，
    # 字典里当然没有（同 create_tag 的新名字不进这里）。
    "update_announcement": ("title", None),
    "delete_announcement": ("title", None),
    # 河灯留言（20260922 第六轮）：按**正文片段**指认（留言没有名字/标题，
    # 用户嘴里说的就是那句话本身）。字段名统一叫 quote，解析器同一套口径。
    "audit_board_comment": ("quote", None),
    "delete_board_comment": ("quote", None),
}


# ── ② 防线：写操作的身份必须落在主人**这句话**里（20260922）───────────────
# 事故现场（20260922 首跑 golden 六条新用例，同一条用例两次跑出两个不同的错）：
#   · 「把那条写着「泠月喵好笨啊」的留言删掉吧」→ planner 填的片段是「好笨」（截短，
#     丢了首尾）；
#   · 「帮我把那条写着「泠月喵真棒！」的留言驳回吧，看着有点乱」→ 填的是「有点乱」
#     （那是主人给的理由，不是那条留言的正文）；
#   · 同一条删除用例另一次跑出的是「河灯留言正文里的一段原话」——**技能描述里的措辞
#     被抄成了参数值**（同 20260921 "举例里不许出现具体取值" 那条教训的镜像）。
# 三次里两次，planner 对"要指认哪一条"这件事的取值不可用；而留言没有标题、名字，
# **正文片段就是它唯一的身份**，填错等于换了个靶子。所以身份不能靠 LLM 转写：
#
# ① `_board_quote_fix`：主人原话里**引号中的那一段**就是身份（"那条写着「X」的留言"
#    ——中文里点一段原文时几乎一定带引号）。planner 的值只要落在某段引号里，就把它
#    校正成**那段引号本身**（顺带治好截短）；一段都没对上、而原话里有引号 → 校正成
#    那唯一一段（同"字面路径修正"的取向：系统数据优先于模型改写）；连引号都没有、
#    值也不在原话里 → **确定性拒绝**（零写 + 如实问他要哪一条，绝不猜"最新的那条"）。
# ② `_ident_grounded`：其余按名字指认的写工具（标签/分类/公告标题/父标签），
#    **免弹窗（同轮命令即确认）的前提**多一条——名字必须在主人这句话里找得到。
#    找不到就不许"一句话直接写"，退回**弹窗**：问句里会把系统解析到的目标写清楚，
#    由主人点一下确定（别名/简称这类合法跳步也在这一步被人类确认，见 _confirm_popup）。
# 边界（如实说明）：地基判据是**子串**级，挡得住"主人从没说过这个名字"，挡不住
# "说过但指的未必是它"（同 20260922 探针报告里那句"亚串免疫"）；真正的身份裁决仍在
# 工具侧的确定性解析（唯一命中才动手，歧义零写）。
_QUOTE_SPAN_RE = re.compile(r"「([^」]{1,80})」|『([^』]{1,80})』|“([^”]{1,80})”"
                            r"|\"([^\"]{1,80})\"")


def _squash_spaces(text) -> str:
    """去掉全部空白——模型转写常把换行/空格抹平（与 tools.base 的片段匹配同一口径）。"""
    return re.sub(r"\s+", "", str(text or ""))


def _msg_quote_spans(user_msg) -> list[str]:
    """主人原话里带引号的片段（按出现顺序，去空白后非空）。"""
    out = []
    for m in _QUOTE_SPAN_RE.finditer(str(user_msg or "")):
        frag = next((g for g in m.groups() if g), "")
        if frag.strip():
            out.append(frag.strip())
    return out


_BOARD_REJECT_WORDS = ("驳回", "隐藏", "不放行", "别显示", "撤下", "下架", "不通过")
_BOARD_PASS_WORDS = ("通过", "放行", "批准", "恢复显示", "放出来", "同意显示")


def _msg_verdict(user_msg) -> str | None:
    """主人这句话里的复核取向（只认**单向**：两边词都出现 = 说不清，返回 None）。

    只给 `_board_quote_fix` 的补参分支用——补出来的 verdict 会**写在弹窗问句里**
    （"人工复核为 驳回（隐藏…）"）由主人确认，所以"认错方向"的代价是一次点取消，
    不是一次错写。
    """
    text = str(user_msg or "")
    rej = any(w in text for w in _BOARD_REJECT_WORDS)
    pas = any(w in text for w in _BOARD_PASS_WORDS)
    if rej == pas:
        return None
    return "reject" if rej else "pass"


def _board_quote_fix(plan_obj: dict, user_msg, rounds: int = 0) -> str | None:
    """留言类写工具的 `quote` 校正到主人引号里那段原话。返回拒绝原因或 None（就地改）。

    单 spec 时校正/拒绝；**零工具**时补参（见下）。两者与 `_write_target_refusal`
    共用同一条边界。
    """
    tools = plan_obj.get("tools") or []
    skill = plan_obj.get("skill") or "chat"
    if not tools:
        # planner 把片段**整丢了**（20260922 实测 2/10：主人引号里明明抄着原话，它却
        # 写下"缺少指认用的正文片段（quote）：不调用任何工具，如实向主人问清" ⇒ 这一轮
        # 什么都不发生，用户看到一句"请说是哪一条"）。主人自己引出来的那一段就是身份，
        # 于是按主人原话补上（弹窗照旧让主人确认，没有静默写）。
        # 只在**首轮**补：后续轮次 planner 看到工具帧之后决定"问一句"可能是对的，
        # 不该被覆盖。
        if rounds or skill not in ("board_audit", "board_delete"):
            return None
        spans = _msg_quote_spans(user_msg)
        if len(spans) != 1:
            return None  # 没引号 / 多段引号：真说不清是哪一条，让 planner 的追问成立
        params = {"quote": spans[0]}
        if skill == "board_audit":
            verdict = _msg_verdict(user_msg)
            if not verdict:
                return None  # 取向也说不清（或两边都说了）→ 不猜
            params["verdict"] = verdict
        logger.info("[planner] 片段通道：planner 零工具追问，但主人引号里有唯一一段原话"
                    "（%r）→ 按主人原话补参", spans[0][:40])
        record("planner", "quote_fill_from_span", skill=skill, quote=spans[0][:60],
               verdict=params.get("verdict"))
        fresh = instantiate_plan(skill, params)
        fresh["params"] = params
        plan_obj.clear()
        plan_obj.update(fresh)
        return None
    if len(tools) != 1:
        return None
    name = _tool_name(tools[0])
    if not name.endswith("_board_comment"):
        return None
    args, args_ok = _tool_args(tools[0])
    if not args_ok or refs.has_refs([{"tool": name, "args": args}]):
        return None
    quote = str(args.get("quote") or "").strip()
    spans = _msg_quote_spans(user_msg)
    sq_quote = _squash_spaces(quote)
    msg = _squash_spaces(user_msg)
    if spans:
        # ① 值落在某段引号里（含截短/加字两种）→ 校正成**那段引号**；多段都含 → 取最长
        #    （最长的那段最能指认；截短版总在长版里）。
        hit = [s for s in spans if sq_quote and sq_quote in _squash_spaces(s)]
        if hit:
            want = max(hit, key=len)
        elif len(spans) == 1:
            # ② 值不在引号里（主人给的理由/planner 的概括）→ 引号那一段才是身份
            want = spans[0]
        else:
            return (f"主人的原话里有 {len(spans)} 段引号（"
                    + "、".join(f"「{s}」" for s in spans[:3])
                    + f"），但都不含系统记下的片段「{quote}」——无法确定要动哪一条留言，"
                      "本次未改动。请说明是哪一段（或把那条留言的原话抄一段给我）")
        if _squash_spaces(want) != sq_quote:
            logger.info("[planner] 留言片段校正：planner 填 %r → 主人引号里的 %r",
                        quote, want)
            record("planner", "quote_correct", tool=name, got=quote[:60], used=want[:60])
            # 重走 instantiate_plan（同"字面路径修正"的做法）：TOOLS 行与**注记**
            # 都从校正后的参数重新生成——只改 spec 字符串的话，narrator 读到的注记
            # 还写着 planner 那个错片段（实测："删除含「泠月」的那条…"）。
            params = dict(plan_obj.get("params") or {})
            params["quote"] = want
            fresh = instantiate_plan(plan_obj.get("skill") or "chat", params)
            fresh["params"] = params
            plan_obj.clear()
            plan_obj.update(fresh)
        return None
    if sq_quote and sq_quote in msg:
        return None  # 没引号但原话里确实有这段 → 保持既有行为（不再加码）
    why = (f"主人这句话里没有能指认那条留言的**正文片段**"
           f"（系统记下的片段是「{quote}」，在主人原话里找不到）"
           if quote else "主人这句话里没有给出那条留言的正文片段")
    return (why + "——本次未改动。留言没有标题，只能按正文里的一段原话指认，"
                  "请把那条留言的原话抄一小段给我（要跟站里一字不差）")


# ── 公告的 title/content：主人标出来的那几句原话才是参数值（②防线续）────────
# 实测（golden `admin_announcement_create_popup` 连跑两跑全红，两种错法）：
#   · 主人说「标题叫「今晚维护」，正文写：今晚 23 点开始维护」→ planner 填
#     `title=公告 / content=公告`（把话里的**名词**当成了参数值）；
#   · 另一跑它自己撰写了一篇像样的公告（「维护通知」/「系统将于今晚进行例行维护…
#     敬请谅解」）——主人给的原话被换成了 LLM 的文案。
# 技能描述里"正文只写用户说过的内容、不许润色、不许自己编一句凑上"两句都在，
# 但它照旧这么干：公告是**一律弹窗**族（authz._ALWAYS_CONFIRM_TOOLS），主人签字前
# 看得见内容——可"签一份自己没写过的东西"正是最该被确定性挡掉的错。故取
# **确定性校正**（而非拒绝）：主人自己标了「标题叫…」「正文写：…」时，那两段话
# 就是参数值，planner 的转写一律让位。
# 边界（如实说明）：只认**带标记**的那两段，没有标记就不动手——"帮我发个公告说
# 今晚维护"这类由 planner 组织措辞是合理的（弹窗照旧让主人过目）；标记之后若还跟着
# 别的指示（"正文写：今晚维护，标题你看着办"），那截尾巴会被一并当成正文（弹窗里
# 看得到，主人可以点取消）——这是标记式抽取的固有代价，选它是因为它从不**编造**。
_ANN_TITLE_RE = re.compile(
    r"(?:标题|题目|名字|名称)\s*(?:叫|叫做|是|为|：|:)\s*[「『“\"]([^」』”\"]{1,60})[」』”\"]")
_ANN_BODY_RE = re.compile(
    r"(?:正文|内容)\s*(?:改成|改为|换成|变成|更新为|更新成|写|是|为|说|：|:)"
    r"\s*[:：]?\s*(.+)", re.S)


def _msg_marked_field(user_msg, kind: str) -> str | None:
    """主人原话里**自己标出来的**标题 / 正文（`标题叫「X」` / `正文写：…`）；没有标 → None。"""
    text = str(user_msg or "")
    if kind == "title":
        m = _ANN_TITLE_RE.search(text)
        return m.group(1).strip() if m else None
    m = _ANN_BODY_RE.search(text)
    if not m:
        return None
    return m.group(1).strip().strip("「」『』“”\"") or None


def _announcement_text_fix(plan_obj: dict, user_msg) -> None:
    """公告的 `title`/`content` 校正到主人写下的原话（就地改；无标记/多 spec 不动）。

    · create：`title` 认「标题叫「X」」标记，`content` 认「正文写：…」标记；
    · update/delete：`title` 是**要动的那条**的身份，只认**唯一一段引号**
      （改公告那句话里常有两段引号——旧标题与新标题，指向谁并不唯一）。
    """
    tools = plan_obj.get("tools") or []
    if len(tools) != 1:
        return
    name = _tool_name(tools[0])
    if name not in ("create_announcement", "update_announcement",
                    "delete_announcement"):
        return
    args, args_ok = _tool_args(tools[0])
    if not args_ok or refs.has_refs([{"tool": name, "args": args}]):
        return
    fixed: dict[str, str] = {}
    if name == "create_announcement":
        want = _msg_marked_field(user_msg, "title")
        got = str(args.get("title") or "").strip()
        if want and _squash_spaces(got) != _squash_spaces(want):
            fixed["title"] = want
    else:
        # 改/删公告的 `title` 是**要动的那条**的身份。主人指认它时几乎一定带着引号
        # （「把标题是「X」的那条公告删掉」），而 planner 会把身份填成描述里的字面量
        # ——20260922 实测：「要删掉的那条公告的标题」被原样填进参数，预检据此报
        # "站内没有这个标题"，主人拿到一句**引着系统自己占位符**的答复。
        # 只认**唯一一段引号**：改公告那句话里常有两段（旧标题 + 新标题），
        # "标题叫「Y」"指向谁并不唯一，宁可不动（交给既有链路如实说"没有这个标题"）。
        spans = _msg_quote_spans(user_msg)
        got = str(args.get("title") or "").strip()
        if len(spans) == 1 and _squash_spaces(got) != _squash_spaces(spans[0]) \
                and _squash_spaces(got) not in _squash_spaces(spans[0]):
            fixed["title"] = spans[0]
    want_body = _msg_marked_field(user_msg, "body")
    got_body = str(args.get("content") or "").strip()
    if want_body and _squash_spaces(got_body) != _squash_spaces(want_body):
        fixed["content"] = want_body
    if not fixed:
        return
    logger.info("[planner] 公告字段校正（%s）：%s → %s", name,
                {k: str(args.get(k))[:30] for k in fixed},
                {k: v[:30] for k, v in fixed.items()})
    record("planner", "announcement_text_correct", tool=name,
           got={k: str(args.get(k))[:60] for k in fixed},
           used={k: v[:60] for k, v in fixed.items()})
    # 重走 instantiate_plan：TOOLS 行与**注记**都从校正后的参数重新生成（同
    # `_board_quote_fix`：只改 spec 字符串的话，注记里还是 planner 那个错值）。
    params = dict(plan_obj.get("params") or {})
    params.update(fixed)
    fresh = instantiate_plan(plan_obj.get("skill") or "chat", params)
    fresh["params"] = params
    plan_obj.clear()
    plan_obj.update(fresh)


# ── 名字通道的目标名：主人引号里的那一段才是它（②防线续二）──────────────────
# 实测（20260922 golden `admin_tag_move_unresolved_target_honest` 八跑）：主人说
# 「把标签「绝对不存在的标签名xyz」挪到「编程」下面」，planner 把这个名字**抄短了**
# ——两跑分别填成「绝对」和「标签名」。这个名字**要写进如实答复里**（"站内没有叫
# 「X」的标签"），于是答复答的是**另一个名字**：主人问的是「绝对不存在的标签名xyz」，
# 系统回"没有叫「绝对」的"——这句话本身是错的（它没说清自己查的是什么）。
# 边界与留言的 `quote` 同源：引号是主人自己下的指认标记，标记内的字**原样**是他说的，
# planner 的转写一律让位。**证据不唯一就不动**（宁可让预检照 planner 的值如实回话，
# 也不猜一个名字去查）。
_NAME_TARGET_TOOLS = ("update_tag", "delete_tag", "update_category",
                      "delete_category", "update_announcement",
                      "delete_announcement")

# "另一个操作数"的标记词：紧跟在它后面的那段引号**不是**目标，而是父标签
# （挪到…下面）或新名字（改名叫…）。语序本身就是主人给的标记——20260922 实测另一跑
# planner 直接把目标名写成「未命名标签」（站内没有这个标签，纯粹是它自己编的占位
# 名字），连 parent_tag 都没填：光靠 planner 的参数已经认不出目标，只有这句话的
# 语序还认得出（"挪到「编程」下面"里的「编程」是父，「绝对不存在的标签名xyz」是目标）。
# 标记词分两族（20260922 ②防线续三）：它们领着的那段引号落在**哪个参数**上取决于族别——
# move（挪到/移到…下面是父标签）与 rename（改名叫/改成…是新名字）。搞混族别就会把新名字
# 填进父标签，所以下面按族别校正、不按"引号里剩下哪段"猜。
_MOVE_MARKS = ("挪到", "移到", "移动到", "放到", "挂到", "换到", "改到", "调到", "调整到")
_RENAME_MARKS = ("改名叫", "改名为", "改名成", "改成", "换成", "改为")
_MOVE_MARK_RE = re.compile(r"(?:" + "|".join(_MOVE_MARKS) + r")\s*$")
_RENAME_MARK_RE = re.compile(r"(?:" + "|".join(_RENAME_MARKS) + r")\s*$")
# `把「X」改名叫「Y」`：改名标记**跟在**某段引号后面 ⇒ 那段引号是**目标**不是值。
_RENAME_AHEAD_RE = re.compile(r"\s*(?:" + "|".join(_RENAME_MARKS) + r")")
# 这句话有没有"改名"意图（比 `_RENAME_MARKS` 宽一点：口语的"改个名"也算）。
_RENAME_INTENT_RE = re.compile(r"(?:" + "|".join(_RENAME_MARKS) + r"|改(?:个)?名)")

# 名词标记：主人点一个"已有名字"的事物时用的词（标签/分类）。免引号形态下，目标名就在
# 名词与动作词之间（见 `_bare_target_name`）。
_TARGET_NOUNS = ("一级标签", "二级标签", "标签", "分类")
# 认"名字"的动作词比认"另一个操作数"的宽：删除也算（"把标签 Asyncio 删掉"同一条语序）。
# 它只用于**取名字**，不进 `_marked_operand`（删掉后面没有"另一个操作数"）。
_DELETE_MARKS = ("删掉", "删除", "移除", "去掉", "撤下", "下架")
_TARGET_ACTION_MARKS = _MOVE_MARKS + _RENAME_MARKS + _DELETE_MARKS
_TARGET_NOUN_RE = re.compile(
    r"(?:" + "|".join(_TARGET_NOUNS) + r")\s*(.+?)\s*(?:"
    + "|".join(_TARGET_ACTION_MARKS) + r")")
# planner 从技能/参数描述里抄下来的**泛称**（20260922 全量回归实测取值：name="标签"、
# parent_tag="父标签名"）——它们不是主人的名字，即便恰好是这句话的子串也不算"有据"。
# 同 20260921「对模型的举例里不许出现具体取值」那条教训的镜像：描述里的措辞会被抄成参数值。
_GENERIC_NAME_WORDS = ("标签", "分类", "一级标签", "二级标签", "标签名", "分类名",
                       "名称", "名字", "目标标签", "目标分类", "这个标签", "这个分类")


def _marked_operand(user_msg, spans: list[str]) -> tuple[str, str]:
    """主人原话里被"另一个操作数"标记词领着的那段引号，以及标记词的族别。

    返回 `(那一段引号, "move"|"rename")`；没有则 `("", "")`。两个族的标记词**同时**
    贴在同一段引号前（"改成 X 再挪到「Y」下面"这类绕法）→ 说不清族别，不动。
    """
    text = str(user_msg or "")
    for m in _QUOTE_SPAN_RE.finditer(text):
        frag = next((g for g in m.groups() if g), "").strip()
        if not frag or not any(_squash_spaces(frag) == _squash_spaces(s) for s in spans):
            continue
        head = text[:m.start()]
        mv, rn = bool(_MOVE_MARK_RE.search(head)), bool(_RENAME_MARK_RE.search(head))
        if mv != rn:
            return frag, ("move" if mv else "rename")
    return "", ""


def _marked_other_operand(user_msg, spans: list[str]) -> str:
    """主人原话里被"另一个操作数"标记词领着的那一段引号（没有则空串）。"""
    return _marked_operand(user_msg, spans)[0]


def _bare_target_name(user_msg) -> str:
    """主人原话里**没加引号**的目标名：名词标记与动作标记之间的那一段。

    20260922 全量回归现场（`admin_tag_move_popup` 五跑一红）：主人说「帮我把标签
    Asyncio 挪到「编程」下面」——要挪的那个名字 **没加引号**，唯一一段引号是父标签，
    而 planner 把目标名抄成了描述里的泛称（`name="标签"`，它甚至是这句话的子串，
    子串级地基放它过去）。语序在这里是主人给的标记：名词与动作词之间那一段就是目标名。

    只认**唯一且干净**的候选：跨小句（有标点）、含别的名词/动作标记、超长、就是泛称
    → 一律返回空串（说不清就不动，与 `_owner_target_span` 同一条边界）。
    """
    text = str(user_msg or "")
    if len(_TARGET_NOUN_RE.findall(text)) != 1:
        return ""  # 一句话里点了不止一个名字（"把标签 A 删掉，再把标签 B 挪到…"）→ 说不清
    m = _TARGET_NOUN_RE.search(text)
    if not m:
        return ""
    raw = m.group(1).strip().strip("「」『』“”\"'").strip()
    if not raw or len(raw) > 60 or raw in _GENERIC_NAME_WORDS:
        return ""
    if any(ch in raw for ch in "，,。；;、！？!?～~"):
        return ""
    if any(w in raw for w in _TARGET_NOUNS + _TARGET_ACTION_MARKS):
        return ""
    # "Asyncio 这个名字" 这类补语：多出来的是主人的解释，不是名字的一部分——一出现就
    # 说不清边界（"抄短了"的对照判据会把整段当成名字，那还不如不动）。
    if any(w in raw for w in ("这个", "那个", "名字", "名称")):
        return ""
    return raw


def _owner_target_span(got: str, spans: list[str], parent: str,
                       other_marked: str = "") -> str | None:
    """主人引号里哪一段是**目标名**？证据不唯一 → None（见上方长注）。

    ① planner 写的名字落在**唯一一段**引号里（抄短了/概括了）→ 那一段就是它；
    ② 引号里有一段被"另一个操作数"的标记词领着（`挪到「B」下面` / `改名叫「B」`
       ——见 `_marked_other_operand`）→ 剩下的那**唯一一段**就是目标；
    ③ 两段引号、其中一段正是 planner 填的父标签名 → 另一段是目标。
    刻意**不做**"只有一段引号就把目标改成它"——「帮我把标签 Asyncio 挪到「编程」
    下面」只有一段引号（是父标签），那样改会把要挪的标签改成父标签本身。
    """
    sq = _squash_spaces(got)
    hits = [s for s in spans if sq and sq in _squash_spaces(s)]
    if len(hits) == 1:
        return hits[0]
    if other_marked:
        rest = [s for s in spans if _squash_spaces(s) != _squash_spaces(other_marked)]
        if len(rest) == 1:
            return rest[0]
    if len(spans) == 2:
        sqp = _squash_spaces(parent)
        if sqp:
            ph = [s for s in spans if sqp in _squash_spaces(s)]
            if len(ph) == 1:
                other = next(s for s in spans if s is not ph[0])
                return other
    return None


def _name_target_fix(plan_obj: dict, user_msg) -> None:
    """按名字指认的写工具：目标名校正到主人引号里那一段（就地改；不动别的参数）。"""
    tools = plan_obj.get("tools") or []
    if len(tools) != 1:
        return
    name = _tool_name(tools[0])
    if name not in _NAME_TARGET_TOOLS:
        return
    tkey, pkey = _WRITE_NAME_FIELDS.get(name) or (None, None)
    if not tkey:
        return
    args, args_ok = _tool_args(tools[0])
    if not args_ok or refs.has_refs([{"tool": name, "args": args}]):
        return
    got = str(args.get(tkey) or "").strip()
    if not got:
        return
    spans = _msg_quote_spans(user_msg)
    other, kind = _marked_operand(user_msg, spans)
    want = _owner_target_span(got, spans, args.get(pkey) if pkey else "", other)
    if not want:
        # 免引号形态（"帮我把标签 Asyncio 挪到「编程」下面"）：目标名在名词与动作词之间。
        # planner 的值**在主人这句话里有据**（逐字说过、且不是泛称、也不是这段的截断）
        # 就不动——防线不是重写器。
        cand = _bare_target_name(user_msg)
        _gq, _cq = _squash_spaces(got), _squash_spaces(cand)
        # 第三种让位的形态：planner 把名字**抄短了**（实测 name="Async"——它是原话的
        # 子串，子串级地基照样放它过去）。取向与引号那条一致：主人原话里那一段是系统
        # 数据，模型的截断让位。只在"捕获段确实更长"时用，且捕获段里不许混补语。
        _frag = bool(_cq and _cq != _gq and _gq in _cq)
        if cand and _cq != _gq and (_gq not in _squash_spaces(user_msg)
                                    or got in _GENERIC_NAME_WORDS or _frag):
            want = cand
    # 父标签走同一条地基：planner 填的父标签不在主人这句话里，而主人用"挪到/移到「X」
    # 下面"给了**唯一**一个候选 → 用它（实测它填的是描述里的「父标签名」「分类」，
    # 弹窗问句于是问的是"移到「父标签名」下面"——主人核对不出来，点了确定就是挂错爸爸）。
    pv = str(args.get(pkey) or "").strip() if pkey else ""
    fix_parent = bool(pkey == "parent_tag" and other and kind == "move" and pv
                      and _squash_spaces(pv) != _squash_spaces(other)
                      and _squash_spaces(pv) not in _squash_spaces(user_msg))
    if (not want or _squash_spaces(want) == _squash_spaces(got)) and not fix_parent:
        return
    # 重走 instantiate_plan：TOOLS 行与**注记**都从校正后的参数重新生成（同
    # `_board_quote_fix`：只改 spec 字符串的话，注记里还是 planner 那个错值）。
    params = dict(plan_obj.get("params") or {})
    if want and _squash_spaces(want) != _squash_spaces(got):
        logger.info("[planner] 目标名校正（%s）：%r → %r", name, got, want)
        record("planner", "name_target_correct", tool=name, got=got[:60], used=want[:60])
        params[tkey] = want
    if fix_parent:
        logger.info("[planner] 父标签校正（%s）：%r → %r", name, pv, other)
        record("planner", "name_target_correct", tool=name, field=pkey,
               got=pv[:60], used=other[:60])
        params[pkey] = other
    fresh = instantiate_plan(plan_obj.get("skill") or "chat", params)
    fresh["params"] = params
    plan_obj.clear()
    plan_obj.update(fresh)


# ── ② 防线续五：写参数里的**名字值**（新名字 / 标签名列表 / 父标签）────────────
# 事故现场（20260922 活体写探针，四条腿同时命中，且**每一条的技能都选对了**）：
#   · 「新建一个一级标签，名字叫「_探针_0922134554」」→ planner 第一轮写
#     `title="20260922_1345_test_tag"`——拿注入的 current_time 拼出来的假名字，
#     **真的建进了生产**（tag id=34），第二轮才建对的那个；
#   · 「一级标签，名字叫_探针色_…，使用粉色颜色」→ `title="名字"`（抄自参数描述
#     `{"title": "新标签的名字"}`）⇒ 弹窗问「新建一级标签「名字」」，主人点确定即落库；
#   · 「新建一个分类，叫「探针分类0922134609」」→ 先 `title="名字"` 建了一个分类、
#     再 `title="探针分类0922"`（把主人的名字截成月日）又建了一个；
#   · 「给文章 1 加上「音乐」标签」→ `add=["标签名"]`（工具侧拒了，但如实答复里出现了
#     "站内并没有叫「音乐」的现成标签"这句**假话**——音乐 id=11 明明在站里）。
# 结构性根因：目标名有两道地基（`_write_target_refusal` 查字典证明它存在 +
# `_ident_grounded` 卡免弹窗），而**新名字**天然不在字典里——`_WRITE_NAME_FIELDS`
# 里 create_tag 的注释写着"新建不在此列"，于是 `title` 这类字段**没有任何判据看它
# 一眼**，而"新建一个标签"正是命令式措辞、走的还是免弹窗快道。⇒ planner 编一个名字，
# 系统就写一个名字（"写操作的目标/身份不许是 LLM 发明"这条不变量在新建面上漏了一格）。
#
# 判据 = **抽取优先于校验**。"值必须在主人这句话里找得到"是子串级，挡不住截断
# （实测 `探针分类0922` 恰恰是主人那句的子串）、也挡不住泛称（`名字` 同样是子串）：
#   ① 主人标出来的那一段（命名标记 `名字叫…` 后面那段 / 引号段，排除父标签等
#      "另一个操作数"）**就是**值——planner 的转写一律让位（造名/截断/泛称一并治好）；
#   ② 抽不出唯一证据时：planner 的值在主人原话里逐字有据、且不是泛称 → 不动
#      （防线不是重写器）；
#   ③ 否则 → **确定性拒绝**（零工具零写 + 如实说"系统填的这个值在主人这句话里
#      找不到来源"），并交代那个字面是**系统自己的参数值**、不许讲成主人说的名字。
# 边界与既有防线同源：子串/标记级，挡不住"说过但未必是它"（亚串免疫）；真正的裁决
# 仍在工具侧（同名不给建、名字对不上拒绝写）与弹窗（主人签字前看得见）。
_WRITE_VALUE_FIELDS = {
    # 工具名 -> 值字段（主人这句话里必须能找到来源的**值**：新名字 / 标签名列表）。
    # 与 `_WRITE_NAME_FIELDS` 分开：那个是"目标身份"（工具要查字典证明它存在），
    # 这个含**新建**的名字——它天然不在字典里，只可能来自主人的原话。
    "create_tag": ("title",),
    "create_category": ("title",),
    "update_tag": ("new_title",),
    "update_category": ("new_title",),
    "set_article_tags": ("add", "remove", "replace"),
}
# 命名标记：主人给"新名字"时用的词。长的在前（同一位置优先匹配更具体的那个）。
# `叫` 单字放最后：它出现在别处的机会最多，靠捕获段的干净度判据兜底。
_NAME_MARKS = ("名字叫", "名字叫做", "名字是", "名字为", "名叫", "名为", "叫做",
               "称为", "标题叫", "标题是", "标题为",
               # 改名族（20260922 续六）：`把标签「X」改名叫「Y」` 里 Y 就是新名字，
               # 与"新建时起名"是同一类命名证据（`名叫` 能擦边命中，但 `改成/改为`
               # 这类不带"名/叫"的形态必须显式列上）。
               "改名叫", "改名为", "改名成", "更名叫", "更名为", "改成", "改为", "换成",
               "叫")
_NAME_MARK_RE = re.compile(r"(?:" + "|".join(_NAME_MARKS) + r")\s*[:：]?\s*")
_NAME_VALUE_STOP = "，,。；;、！？!?～~\n"
# 捕获段的干净度判据（与 `_bare_target_name` 同源，另加"父标签"这一族泛称）
_GENERIC_VALUE_WORDS = _GENERIC_NAME_WORDS + (
    "父标签", "父标签名", "父级标签", "上级标签", "新名字", "新标签", "标题", "题目")
# 父标签的语序标记：`在「编程」下面/里` 与 `挪到「编程」下面` 两种领法
_PARENT_TAIL_RE = re.compile(r"\s*(?:下面|底下|之下|下|里|内|中)")


def _value_clean(raw: str) -> str:
    """捕获段过一遍干净度判据：脏（带标点/超长/是泛称/混着名词或动作词）→ 空串。"""
    raw = str(raw or "").strip().strip("「」『』“”\"'").strip()
    if not raw or len(raw) > 60 or raw in _GENERIC_VALUE_WORDS:
        return ""
    if any(ch in raw for ch in _NAME_VALUE_STOP):
        return ""
    # 名词只在**段首**算脏（"标签"/"一级标签"/"分类名"是名词短语形态）；名字里**含**名词
    # 是常见形态——实测 `把分类「探针分类0922193132」改名叫「探针分类0922193132R」` 里那段
    # 正确的新名字被整段判脏（`分类` 恰好在词汇里）⇒ 值空缺、回落到目标那段 ⇒ 改名被写成
    # 一次空转。动作词仍按"含"判（名字里出现 `挪到/改名叫` 基本只可能是误捕获；判脏的代价
    # 是零写 + 如实追问，方向安全）。
    if raw.startswith(_TARGET_NOUNS) or any(w in raw for w in _TARGET_ACTION_MARKS):
        return ""
    if any(w in raw for w in ("这个", "那个", "名字", "名称")):
        return ""
    return raw


def _msg_named_value(user_msg) -> str:
    """主人原话里"给新名字"的那一段（`名字叫 X` / `叫「X」`…）；没有则空串。

    捕获段在**第一个停顿符**处截断（"名字叫Redis，颜色粉色" → Redis），两端引号剥掉。
    """
    text = str(user_msg or "")
    m = _NAME_MARK_RE.search(text)
    if not m:
        return ""
    raw = text[m.end():]
    for stop in _NAME_VALUE_STOP:
        i = raw.find(stop)
        if i >= 0:
            raw = raw[:i]
    return _value_clean(raw)


def _value_candidate_spans(user_msg) -> list[str]:
    """主人这句话里可以当"名字值"的那几段引号（排除父标签/目标/另一个操作数那几段）。

    排除三种领法：前面贴着 `挪到` 的（那是父标签，另一个操作数）、后面跟着 `改名叫`
    的（那是**目标**，值在标记**后面**那段）、后面跟着 `下面/里` 的（那是父标签）。
    `在「编程」下面加一个二级标签，名字叫 X` 里的「编程」正是靠最后一条被排除，否则
    新标签会被起名叫「编程」。
    """
    text = str(user_msg or "")
    out: list[str] = []
    for m in _QUOTE_SPAN_RE.finditer(text):
        frag = next((g for g in m.groups() if g), "").strip()
        if not frag:
            continue
        if _MOVE_MARK_RE.search(text[:m.start()]):
            continue
        if _RENAME_AHEAD_RE.match(text[m.end():]):
            continue
        if _PARENT_TAIL_RE.match(text[m.end():]):
            continue
        out.append(frag)
    return out


def _parent_marked_span(user_msg) -> str:
    """主人这句话里被"父标签"语序标出来的那**唯一**一段引号；说不清则空串。"""
    text = str(user_msg or "")
    hits: list[str] = []
    for m in _QUOTE_SPAN_RE.finditer(text):
        frag = next((g for g in m.groups() if g), "").strip()
        if not frag:
            continue
        if _MOVE_MARK_RE.search(text[:m.start()]) or _PARENT_TAIL_RE.match(text[m.end():]):
            hits.append(frag)
    return hits[0] if len(hits) == 1 else ""


def _grounded_value(val, sq_msg: str) -> bool:
    """这个值在主人原话里逐字有据吗？泛称/描述里的措辞**不算**有据。"""
    v = _squash_spaces(val)
    if not v or v in _GENERIC_VALUE_WORDS:
        return False
    return v in sq_msg


def _name_arg_fix(plan_obj: dict, user_msg) -> tuple[str, str] | None:
    """写参数里的名字值校正到主人的原话（就地改）；校正不了则返回 `(工具名, 原因)`。

    只管**值**字段（`_WRITE_VALUE_FIELDS`）与 `parent_tag`——目标字段是
    `_name_target_fix` 的地盘，两者分工不重叠。
    """
    tools = plan_obj.get("tools") or []
    if len(tools) != 1:
        return None
    tool = _tool_name(tools[0])
    vfields = _WRITE_VALUE_FIELDS.get(tool) or ()
    tkey, pkey = _WRITE_NAME_FIELDS.get(tool) or (None, None)
    if not vfields and not pkey:
        return None
    args, args_ok = _tool_args(tools[0])
    if not args_ok or refs.has_refs([{"tool": tool, "args": args}]):
        return None
    msg = str(user_msg or "")
    sq = _squash_spaces(msg)
    named = _msg_named_value(msg)
    # 主人这句话里有没有"改名"意图（`改名叫/改成/改为/换成…`，含口语的"改个名"）。
    # 只给下面那条"新名字 == 目标自己"的判据当闸用：没有改名意图时，同名 new_title
    # 是 planner 的冗余填充，不该把一次真移动拦下来。
    rename_intent = bool(_RENAME_INTENT_RE.search(msg))
    # 这句话里标出了**父标签**（`在「编程」下面`）→ 引号段是父，不是新名字的候选：
    # 一旦把它当值，新标签就会被起名叫「编程」（实测 `在「编程」和「摄影」下面都建一个`
    # 这类句子正落在这里）。没有命名标记时宁可拒绝，也不拿父名当新名。
    parent_hint = _parent_marked_span(msg)
    cand_spans = [] if parent_hint else _value_candidate_spans(msg)
    fixed: dict[str, object] = {}
    unresolved: list[str] = []
    selfsame: list[str] = []

    for key in vfields:
        cur = args.get(key)
        if cur in (None, "", [], {}):
            continue
        if isinstance(cur, (list, tuple)):
            vals = [str(v) for v in cur]
            bad = [v for v in vals if not _grounded_value(v, sq)]
            if not bad:
                continue
            pool = [s for s in cand_spans
                    if _squash_spaces(s) not in {_squash_spaces(v) for v in vals}]
            if not pool:
                unresolved.extend(bad)
            elif len(bad) == 1:
                fixed[key] = [pool[0] if _squash_spaces(v) == _squash_spaces(bad[0])
                              else v for v in vals]
            elif len(bad) == len(pool):
                it = iter(pool)
                fixed[key] = [next(it) if v in bad else v for v in vals]
            else:  # 说不清哪个对上哪个 → 不猜
                unresolved.extend(bad)
            continue
        got = str(cur).strip()
        want = named or (cand_spans[0] if len(cand_spans) == 1 else "")
        tgt_name = _squash_spaces(str(args.get(tkey) or "")) if tkey else ""
        if want and tgt_name and _squash_spaces(want) == tgt_name:
            # 抽出来的"值"就是**目标自己**：等于没抽出来（实测现场见下）——丢掉它，
            # 让下面两条判据接着说话（有正确的命名证据时优先校正，没有才追问）。
            want = ""
        if want and _squash_spaces(want) != _squash_spaces(got):
            fixed[key] = want
        elif (tgt_name and rename_intent and _squash_spaces(got) == tgt_name):
            # 要写进去的"新名字"就是它**自己现在的名字**——一次注定空转的改名：工具照写、
            # 回执写"X → X"、库一个字节没动，而回执读起来像改成功了。实测现场（探针腿⑭
            # 20260922）：目标名恰是新名字的**前缀**时，抽取落回目标那段、planner 写对的
            # 值被覆写成目标名。这种形态没有任何命名证据，不猜：零写 + 如实追问。
            # （只在主人这句话里**有改名意图**时才判——移动类命令里 planner 顺手带上
            # 同名 new_title 是无害冗余，不能因此把一次真移动拦掉。）
            selfsame.append(got)
        elif not _grounded_value(got, sq):
            unresolved.append(got)

    pv = str(args.get(pkey) or "").strip() if pkey else ""
    if pkey == "parent_tag" and pv and not _grounded_value(pv, sq):
        pcand = _parent_marked_span(msg)
        if pcand and _squash_spaces(pcand) != _squash_spaces(pv):
            fixed[pkey] = pcand
        elif not fixed:
            unresolved.append(pv)

    if unresolved or selfsame:
        if selfsame and not unresolved:
            got_txt = "」「".join(dict.fromkeys(selfsame))
            why = (f"主人这句话里没有能当「新名字」的那一段——唯一对得上的是"
                   f"**要改的那条自己现在的名字**「{got_txt}」，照字面执行就是一次"
                   f"没有改动的空转，所以没有动手")
        else:
            got_txt = "」「".join(dict.fromkeys(unresolved))
            why = (f"主人这句话里没有能对上「{got_txt}」这个参数值的名字"
                   f"（它既不是主人逐字说过的名字，也不是主人标出来的任何一段"
                   f"——命名标记后面那段、引号里那几段，都对不上）")
        logger.warning("[planner] 写参数值在主人原话里找不到来源（%s）：%s → 确定性如实收尾",
                       tool, why)
        record("planner", "write_value_unresolved", tool=tool,
               values=list(dict.fromkeys(unresolved or selfsame))[:3], round=None)
        return tool, why
    if not fixed:
        return None
    params = dict(plan_obj.get("params") or {})
    logger.info("[planner] 写参数名字值校正（%s）：%s", tool,
                {k: str(params.get(k))[:30] for k in fixed})
    record("planner", "write_value_correct", tool=tool,
           got={k: str(params.get(k))[:60] for k in fixed},
           used={k: str(v)[:60] for k, v in fixed.items()})
    params.update(fixed)
    fresh = instantiate_plan(plan_obj.get("skill") or "chat", params)
    fresh["params"] = params
    plan_obj.clear()
    plan_obj.update(fresh)
    return None


def _ident_grounded(name: str, args: dict, user_msg) -> bool:
    """写操作的身份参数是否落在主人这句话里（见本小节头注 ②）。

    20260922 续五：`_WRITE_VALUE_FIELDS` 的**值**字段（新名字 / 标签名列表）一并
    纳入——免弹窗（同轮命令即确认）的前提从"目标说过"扩到"要写进去的值也说过"。
    校正（`_name_arg_fix`）在这之前跑过，所以这里判的是校正后的参数。
    """
    tkey, pkey = _WRITE_NAME_FIELDS.get(name) or (None, None)
    extra = tuple(_WRITE_VALUE_FIELDS.get(name) or ())
    if not tkey and not pkey and not extra:
        return True  # 不是按名字指认的写工具（文章族走 target_* 三条判据）
    msg = _squash_spaces(user_msg)
    if not msg:
        return False
    for key in (tkey, pkey) + extra:
        if not key:
            continue
        val = args.get(key)
        if isinstance(val, (list, tuple)):
            vals = [str(v) for v in val if str(v).strip()]
            if any(_squash_spaces(v) not in msg for v in vals):
                return False
            continue
        val = _squash_spaces(val)
        if val and val not in msg:
            return False
    return True


def _write_target_refusal(plan_obj: dict, config) -> tuple[str, str] | None:
    """本轮写操作的目标名字能否唯一落到站内一行？返回 `(工具名, 拒绝说明)` 或 None。

    与工具**同一套解析**（`tools.base._find_named_tag` / `_find_named_category`），
    并且目标与父标签用**同一份字典快照**查。这一层只回答"这件事现在做得成吗"，
    真做的时候工具仍会自己再读一次字典——两次判断互不背书，谁都不替对方下结论。
    """
    tools = plan_obj.get("tools") or []
    if not tools:
        return None
    name = _tool_name(tools[0]) if len(tools) == 1 else None
    if name not in _WRITE_NAME_FIELDS:
        # 多 spec 混排 / 不是按名字的写工具：不在这里判（今天写轮一次只展开一条，
        # 真出现混排也该由工具自己如实拒绝，而不是被这层拦成一个"做不了"）。
        return None
    args, args_ok = _tool_args(tools[0])
    if not args_ok or refs.has_refs([{"tool": name, "args": args}]):
        return None
    from tools.base import (_announcement_index, _board_index, _category_index,
                            _find_board_comment, _find_named_announcement,
                            _find_named_category, _find_named_tag, _tag_index)
    tkey, pkey = _WRITE_NAME_FIELDS[name]
    is_cat = name.endswith("_category")
    is_ann = name.endswith("_announcement")
    is_board = name.endswith("_board_comment")
    tag_index = None if (is_cat or is_ann or is_board) else _tag_index(config)
    cat_index = ann_index = board_index = None
    if is_cat:
        cat_index = _category_index(config)
        if cat_index is None:
            return None  # 读不到字典 ≠ 没有：不拦（见上方边界）
    elif is_ann:
        ann_index = _announcement_index(config)
        if ann_index is None:
            return None  # 同上
    elif is_board:
        board_index = _board_index(config)
        if board_index is None:
            return None  # 同上（读不到清单不是"没有这条留言"）
    elif tag_index is None:
        return None
    if tkey:
        want = str(args.get(tkey) or "").strip()
        if want:
            if is_cat:
                hit, err = _find_named_category(want, config, index=cat_index)
            elif is_ann:
                hit, err = _find_named_announcement(want, config, index=ann_index)
            elif is_board:
                hit, err = _find_board_comment(want, config, index=board_index)
            else:
                hit, err = _find_named_tag(want, config, args.get("level"),
                                           index=tag_index)
            if err:
                return name, err
    if pkey:
        pname = str(args.get(pkey) or "").strip()
        if pname:
            hit, err = _find_named_tag(pname, config, "one", role="一级标签",
                                       index=tag_index)
            if err:
                return name, err
    return None


def _confirm_popup(state: AgentState, specs: list, principal, user_msg: str,
                   config) -> dict | None:
    """本轮要不要弹"写操作确认框"？要就返回 `pending_confirm` 的 state 增量。

    触发条件（**全部确定性、无 LLM**）：本轮计划里有写操作卡在同意闸上（用户有
    意向、但这句没被判成明确命令），且这句**不是提问/假设**。生产实测（20260921）
    的死路正是这里：「一级标签，名字叫X，使用粉色颜色」判不成命令 ⇒ 闸不放行 ⇒
    execute 产 consent_required ⇒ planner 追问 ⇒ 用户再打一遍 ⇒ 又判不成 ⇒
    **没有出口**。现在同一个判据的 False 换来的是一次点击，而不是一次往返。

    与"提问/假设"的分界见 authz.is_question_like：用户只是在问（"把文章 12 设为
    私密会有什么影响？"）时绝不能弹窗——那等于把提问读成了意图。

    收集的 spec 同时要过**目标有据**（id 必须本轮读到过/页面上下文/用户点名），
    否则弹出来的是"要不要把文章 12 设为私密"而 12 是编的：确认框会把一个幻觉
    洗成一条已授权的写。没据的走既有 unknown_target 链路（先去读、再回来）。
    """
    grant = state.get("confirm_grant")
    if grant or authz.is_question_like(user_msg):
        return None
    picks: list = []
    for spec in specs:
        name = _tool_name(spec)
        if not authz.requires_consent(principal, name):
            continue
        args, args_ok = _tool_args(spec)
        # 免弹窗（"同轮命令即确认"）多一条前提（20260922 ②防线）：**主人自己把目标
        # 说出口了**。判成命令但目标名字不在主人这句话里（别名跳步、从执行记忆里
        # 拣的名字、模型自己概括的片段）→ 不许一句话直接写，退回弹窗：问句里会把
        # 系统解析到的目标写清楚（标签名/分类名/公告标题/留言原文），由主人点一下确定。
        # 这是**加一次点击**，不是砍能力——名字原样说出口的常见路径一行没变。
        if args_ok and authz.consent_granted(principal, name, user_msg) \
                and _ident_grounded(name, args, user_msg):
            continue  # 明确命令 + 目标地基都在：直接执行，不弹窗
        decision = authz.check(principal, name)
        if not decision.allowed and authz.enforcing(decision.scope):
            continue  # 权限硬拦：弹窗也改不了"这个人不能做"，走既有拒绝链路
        if not args_ok or refs.has_refs([{"tool": name, "args": args}]):
            # 参数没解析出来 / 还挂着 $ref（引用依赖的是签发那一轮的工具帧，执行轮
            # 早已不在）→ 不签发，退回既有错误帧链路让 planner 自己收拾
            continue
        if name in _ARTICLE_WRITE_TOOLS and (
                not A.target_mentioned(
                    args.get("article_id"),
                    _target_evidence(state, user_msg,
                                     _page_ctx(state["messages"], principal.known_role)))
                # 与用户点名不一致 → 同样不弹（20260921 第三轮）：确认框会把目标
                # 明明白白写出来，但问的必须是**主人点的那一篇**——问错一篇再让主人
                # 点确定，等于把误靶洗成一条已授权的写。跳过 → 由下面的循环产
                # target_mismatch 帧，planner 按帧改回来（那条链路本就在等着）。
                or not A.target_named(args.get("article_id"),
                                      A.user_named_article_ids(user_msg))):
            continue
        picks.append({"tool": name, "args": args})
    if not picks:
        return None
    conv_id = (config or {}).get("configurable", {}).get("conversation_id")
    token = confirm.sign(principal.uid, conv_id, _plan_skill(state), picks)
    if not token:
        # 密钥没读到 → 不弹窗（宁可走追问，也不发一个验不过的令牌）。这个兜底**必须
        # 留痕**：它一旦生效，**所有**写确认弹窗会静默消失、退回"判不成命令就追问"
        # 的死路形态，而链路上没有任何别的信号。20260922 CI 实测：无 .env 的环境里
        # `settings.jwt_secret` 是空串 ⇒ 本分支吃掉三条"该弹窗"的正例，本地因有
        # .env 全绿。fail-closed 不变，只是不再无声。
        logger.warning("[confirm] 签发密钥空缺（settings.jwt_secret 为空）→ 本轮不弹确认框：%s",
                       [p.get("tool") for p in picks])
        record("confirm", "token_sign_failed", tools=[p.get("tool") for p in picks])
        return None
    # 问句要把父标签**名字**写出来（20260921）：只写「新建二级标签「Rust」」时
    # 用户无从核对它要挂到哪个爸爸底下，而"挂错父标签"正是本轮修的参数对调事故。
    # 读字典失败 → index=None，问句退回名字原文（宁可只给名字，也不能因为一次
    # 读不到就不弹窗——那会退回"死路"形态）。
    try:
        from tools.base import _tag_index
        tag_index = _tag_index(config)
    except Exception:
        tag_index = None
    # 分类字典只为分类写操作而读（删分类要报"有几篇文章会失去分类"）：一张平表、
    # 一次请求，且**只在真要点到分类时才读**——标签写操作不该为此多一次网络往返。
    cat_index = None
    if any(str(s.get("tool") or "").endswith("_category") for s in picks):
        try:
            from tools.base import _category_index
            cat_index = _category_index(config)
        except Exception:
            cat_index = None
    # 留言清单同理（20260922 第六轮）：留言按正文片段指认，问句要把**匹配到的
    # 那一条**（#id + 作者 + 原文）写出来，否则主人签的是一段"可能出现在好几条
    # 留言里"的话。
    board_index = None
    if any(str(s.get("tool") or "").endswith("_board_comment") for s in picks):
        try:
            from tools.base import _board_index
            board_index = _board_index(config)
        except Exception:
            board_index = None
    # 文章清单同理（20260922 第七轮）：文章写操作的问句此前**只有内部 id**
    # （「修改文章 46」），是整套写面里唯一的盲签——主人看不出那是不是他说的那篇，
    # 而弹窗点确定正是文章这类写操作唯一的人类兜底（"目标有据 ≠ 目标唯一"）。
    # 读的是与写工具读前值同一份后台清单（含草稿/私密、不含修改稿）：弹窗里的
    # 「现在：草稿」必须是真的会被改的那一行的现状。读不到 → 退回只写 id，
    # **绝不因此不弹窗**（那会退回"判成歧义就追问"的死路形态）。
    # 20260923 批 7 补一道前置判据：这份清单挂在**管理员面**，而写面从这一批起
    # 含普通访客也能用的收藏两件——对访客读它只会白拿一次 403。判据用 authz 那张
    # 表现成的（他读不读得动 list_admin_notes），不另立规则。
    note_index = None
    if any(str(s.get("tool") or "") in _POPUP_TITLE_TOOLS for s in picks) \
            and authz.check(principal, "list_admin_notes").allowed:
        try:
            from tools.base import _note_index
            note_index = _note_index(config)
        except Exception:
            note_index = None
    return {
        "pending_confirm": {
            "q": A.render_confirm_question(picks, tag_index, cat_index, board_index,
                                           note_index),
            "opts": [{"label": "确定", "value": "yes", "kind": "primary"},
                     {"label": "取消", "value": "no", "kind": "default"}],
            "token": token,
            "specs": picks,
            "skill": _plan_skill(state),
        },
        "confirm_text": A.render_confirm_text(picks, tag_index, cat_index, board_index,
                                               note_index),
    }


def _plan_skill(state: AgentState) -> str:
    """当前计划的技能名（plan 文本第 1 行 SKILL=…）——令牌里带着它，执行轮据此
    拼计划，**不靠模型回忆**。取不到给空串（sign 会拒绝签发）。"""
    m = re.search(r"SKILL\s*=\s*(\S+)", state.get("plan", "") or "")
    return m.group(1) if m else ""


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
    20260921：写操作确认弹窗——见 _confirm_popup（它只在"有意向但没判成命令"
    时提前 return，判成命令的一律照旧直接执行）。
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
    # 这里的 page_ctx 只喂 _target_evidence/_create_display_text（都是"用户提没提
    # 到这篇文章/给设备写什么文案"），能力清单不参与——但传 role 与另两个节点同构，
    # 免得下次有人复制这行时把角色漏掉。
    page_ctx = _page_ctx(state["messages"], principal.known_role)
    # 用户本轮**原话点名**的文章 id（写侧目标的权威，见下方 target_conflict）：
    # 逐 spec 只读不改，循环外算一次。
    named_ids = A.user_named_article_ids(user_msg)
    # 写操作确认弹窗（20260921）：**执行之前**判，命中就一个工具都不执行、直接回
    # pending_confirm（路由据此去 END，见 route_after_execute）。之所以提前到这里
    # 而不是在下方逐 spec 里：弹窗是一份**整批**的确认（计划里的写操作各自成单，
    # 混排只可能是将来），而"问一句"这件事本身不该以执行一半为代价。
    popup = _confirm_popup(state, specs, principal, user_msg, config)
    if popup is not None:
        record("execute", "consent_popup", principal=str(principal),
               specs=",".join(s["tool"] for s in popup["pending_confirm"]["specs"]))
        logger.info("[execute] 写操作未判成命令 → 弹确认框（零执行）: %s",
                    ",".join(s["tool"] for s in popup["pending_confirm"]["specs"]))
        # receipts 原样带回（本轮零执行，累计值不变）：execute 的 updates 里
        # 这个键是**形状契约**的一部分（多数轮次都带它），缺一次就让"回执累计"
        # 的消费方少一次更新——测试与 server 都按"每轮都有"读它。
        return dict(popup, messages=[], receipts=list(state.get("receipts") or []))
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
        # 传 decision.scope 而不是无参调用：`admin.console` 是不吃 shadow 的硬拦
        # （20260921，见 authz._HARD_SCOPES），其余 scope 仍走全局开关。
        if not decision.allowed and not authz.enforcing(decision.scope):
            record("execute", "authz_shadow", tool=name, principal=str(principal),
                   decision=str(decision))
        # 写操作的「人在回路」确认（20260920，秘书类前置需求 ③）：**权限判"能不能做"，
        # 这里判"这一次用户到底要不要做"**。只对有 CONSENT_SCOPES 声明（写站点内容、
        # 对外可见收不回）的工具生效——今天没有这类工具，所以对现有行为零影响；
        # 一旦新增，它**自动**落在闸下（声明驱动，不靠人记得来改）。与权限判据同层：
        # 确定性、无 LLM、调用之前、fail-closed。今天不设 shadow：这一层是纯新增的
        # 保护，不存在"真流量会被它改行为"的观测需求（没有工具会命中它）。
        # 确认轮（20260921）：`confirm_grant` 在场 = 用户刚在确认框上点了确定，
        # **这一下点击就是同意本身**——不再要求"本轮消息里有一句确认语"（那条
        # 判据是给"用户打字确认"用的）。注意这里放行的只有同意闸：权限（scope）
        # 一行不动，非管理员拿着令牌照样被 _HARD_SCOPES 拦下。
        consent_missing = (authz.requires_consent(principal, name)
                           and not state.get("confirm_grant")
                           and not authz.consent_granted(principal, name, user_msg))
        if consent_missing:
            record("execute", "consent_required", tool=name, principal=str(principal),
                   scope=authz.required_scope(name))
            logger.info("[execute] 写操作未经确认，不执行: %s（principal=%s）",
                        spec, principal)
        # 写操作的目标校验（20260921 第二轮，与上一条同层同风格：确定性、无 LLM、
        # 调用之前、fail-closed）：**"该不该做"之后再判"做哪一篇"**。后台写工具的
        # article_id 必须本轮有据（本轮读过的帧里出现过、页面上下文里是当前文章、
        # 或用户这条消息里点名了这个数字），否则产 unknown_target 帧让 planner
        # 先读再写。挡的是"整轮什么都没读、凭上下文记忆/印象写一个 id"——写错文章
        # 与写错状态不同，它是**不可回滚的对外可见改动**（把别人的文章设成私密）。
        # 之所以放在这里而不是工具内部：工具看不到"本轮读到过什么"（跨轮记忆与
        # 页面上下文都只活在 graph 状态里）。
        # **确认轮放行**（20260921，与上面同意闸同源）：确认轮里没有本轮工具帧
        # （那一轮是用户点的按钮，不是一次检索），"有据"已由**签发令牌时**的解析
        # 保证（见 _confirm_popup：无据的 spec 根本不进令牌）。不放行的话每一次
        # 确认执行都会栽在 unknown_target 上，整个弹窗机制形同虚设。
        target_missing = (ref_err is None and args_ok and name in _ARTICLE_WRITE_TOOLS
                          and not state.get("confirm_grant")
                          and not A.target_mentioned(args.get("article_id"),
                                                     _target_evidence(state, user_msg, page_ctx)))
        if target_missing:
            record("execute", "unknown_target", tool=name,
                   article_id=str(args.get("article_id")))
            logger.warning("[execute] 写操作目标无据，不执行: %s（本轮没读到过这个 id）", spec)
        # 目标与用户点名不一致（20260921 第三轮，活体探针实证）：管理员说「把文章 1
        # 置顶」，planner 填的却是 `list_admin_notes` 返回的**第一行**（id=46）——
        # 上一条判据放行了它（46 确实出现在本轮帧里），但那不是用户点的那一篇。
        # 用户原话点名的 id 是权威：不一致一律不执行（fail-closed），回一条带
        # reason 的帧把"该改哪一篇"讲清楚，让 planner 自己改回来；系统**不改写**
        # planner 填的参数（目标只能由用户决定，系统只否决）。空集=用户没点名
        # （"把这篇置顶"这类指代）→ 判据不启用，行为与之前完全一致。
        target_conflict = (ref_err is None and args_ok and not target_missing
                           and not consent_missing and not state.get("confirm_grant")
                           and name in _ARTICLE_WRITE_TOOLS
                           and not A.target_named(args.get("article_id"), named_ids))
        if target_conflict:
            record("execute", "target_mismatch", tool=name,
                   article_id=str(args.get("article_id")), named=sorted(named_ids))
            logger.warning("[execute] 写目标与用户点名不一致，不执行: %s（点名 %s）",
                           spec, sorted(named_ids))
        # 屏幕文案创作：text 参数缺失/为空 → execute 结合对话创作（技能固有设计）
        if ref_err is None and name == "device_oled_display" and not args.get("text"):
            args = dict(args)
            args["text"] = _create_display_text(user_msg, page_ctx)
        _t_tool = time.monotonic()
        if ref_err:
            out = f"__ERROR__: 参数引用无法解析[{ref_err}]（上一步返回里没有这个值——改参数或换个工具）"
            logger.warning("[execute] 参数引用解析失败，不执行: %s → %s", spec, ref_err)
        elif not decision.allowed and authz.enforcing(decision.scope):
            out = authz.denial_frame(decision, principal)
            logger.warning("[execute] 权限拒绝，不执行: %s → %s", spec, decision)
        elif consent_missing:
            # 与权限拒绝同族（__ERROR__ + 原因码 → blocked 链路），语义是"去问用户"
            out = authz.consent_frame(name, principal)
        elif target_missing:
            # 同族（__ERROR__ + 原因码），语义是"先去读、或先问哪一篇"
            out = A.unknown_target_frame(name)
        elif target_conflict:
            # 同族，语义是"你改错了篇，按主人点名的改回来"
            out = A.target_conflict_frame(name, named_ids, args.get("article_id"))
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
            # 后台写回执（20260921 第二轮，用户拍板"零迁移：写进 detail"）：执行身份
            # 与变更前→后随回执落 execution_log —— 写操作是**不可回滚的对外改动**，
            # 库里必须留得下"谁在什么时候把哪一篇从什么改成什么"。
            # 键名是 Python 写 / Rust 读的跨语言契约（src/routes/chat.rs::render_exec_row），
            # 改一侧必须同步另一侧（与 digest/title 同一处理：两侧测试各锁一遍）。
            # **只落角色，绝不落 uid**：detail 会进生产库、还会被 narrator 念出来。
            if authz.required_scope(name) in authz.AUDIT_SCOPES:
                rcpt["principal_role"] = _principal_of(config).known_role or ""
                for k in _RCPT_META_KEYS:
                    v = (getattr(out, "meta", None) or {}).get(k)
                    if v is not None:
                        # 一律**字符串化**再落回执：跨语言契约里只留一种类型
                        # （Rust 侧统一 as_str() 取）。int/str 混装是"渲染器对着
                        # 一半回执取到空串"的经典来源——args 侧早已按同样理由
                        # 全部 str()（见上面 rcpt 的 args 构造）。
                        rcpt[k] = str(v)[:120]
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

{audience}

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
    就是多此一举**。
16. 不得凭空对"站内有没有"下结论（20260921）：说"站内没有讲这个的文章""没收录
    ""全站查不到相关内容"这类**结论**，前提是依据里看得到检索——本轮的工具帧里有
    检索/读取动作，或"工具执行记录"里有往轮的**检索行**（形如 `站内检索「…」`/
    `搜索「…」`）。本轮没查过就别替站里下结论：要么只用通用知识把问题答清楚
    （**不提**站内），要么如实说"站里我还没查过，要不要我去查一遍"。零工具轮
    凭空说"站内没有"是被系统拦下的（会整轮换成道歉），别让自己撞上去。
17. 颜色一律"中文色名 + 色值"（20260921）：说到站内颜色（标签配色这类）时，
    写成「粉色 #eb2f96」这种**名 + 值**并列的形式——色块由前端按色值渲染，
    **不要自己画方块/符号**（你画的不可能显示成颜色）。只报站内色板里的 8 种：
    蓝 #1677ff、绿 #52c41a、橙 #fa8c16、粉 #eb2f96、紫 #722ed1、青 #13c2c2、
    红 #f5222d、黄绿 #a0d911——色板外的十六进制别说（前端不认，说了主人也看不见）。
18. 要动站内数据的操作（新建标签、改文章状态、收藏/取消收藏、标记通知已读）由系统自己走确认流程——**确认框
    一个字都不要提**：真弹了确认框的那一轮**根本轮不到你说话**（气泡是系统给的
    确定性文案），所以只要你在说话，就是没弹。说"已经发起/已提交/请留意确认弹窗/
    等你点确认"就是编的（20260921 22:02 实测：访客说"把文章 12 设为私密"，叙述
    回了"系统这边已经发起啦…请留意屏幕上的确认弹窗"，而那一轮连帧都没有）。
    你的正文里只允许出现两种东西：工具回执里的事实（成功说成功、失败说失败），
    或者"这件事我做不到／需要主人自己做"。确认之后的那一轮同理，只按回执说结果，
    绝不把"还没动手"讲成"已经办好了"。收藏/取消收藏/标记已读这三件写的是
    **说话人自己**的数据：他把话说成命令时系统**不弹框**、直接做——所以这三件
    **只能按回执说**（回执里没有成功那一行，就是没成功）。
19. 系统说"某个名字没找到"时，**照抄它给的那个字面，别把两个名字画等号**
    （20260922）：系统查的是它**自己填进参数的那个值**，未必是主人嘴上说的那个名字。
    实测：主人说"给文章 1 加上「音乐」标签"，系统用的值是占位文字「标签名」，
    如实答复里于是出现了"站内并没有叫「音乐」的现成标签"这句**假话**（音乐在站里，
    id=11）。凡是"没有叫「X」的"这类结论，X 必须是系统原话里那个字面；分不清就
    原样引述（"系统返回的是「站内没有这个标签：标签名」"），**不许**替系统把
    主人点名的名字和系统查的值说成同一个。
20. "我自己的数据"（收藏 / 未读通知 / 未读汇总）与隐私数据的三种"读不到"必须
    分开说（20260923）：**读到了确实是空** 才准说"你还没有…／没有未读"；工具
    回的是**未登录**（帧里原话「未登录：…」）或**读不到**（服务不可用）时，
    **一个字都不许说成"没有"**——那是把"没读到"讲成"事实是空的"（访客手里
    可能一堆收藏和未读，只是没登录）。逐字照帧说：未登录 → "需要先登录博客
    账号"；读不到 → "这次没读到，不敢下结论"。同一条也管"改没改成功"：收藏/
    取消收藏/标记已读是否生效，**只认本轮执行回执**——回执里写的是「本次改动
    未确认生效」时把这句如实转述，不许翻译成"已经帮你收藏好啦/已标记为已读"。"""


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
    # 本轮对话者是访客还是主人本人（20260921）：纪律文本只有一份，只有"对话者是谁"
    # 与称呼/口径按角色变。未知角色 → 访客那段（fail-closed：宁可把主人当访客，
    # 也不把访客当主人——后者会用主人的口径去答权限相关的事）。
    role = _principal_of(config).known_role
    system = SystemMessage(content=_EXECUTOR_PROMPT.format(
        persona=BLOG_ASSISTANT_PROMPT,
        audience=audience_block(role),
        plan=state["plan"],
        tool_frames=_frame_texts(state["messages"]),
        exec_receipts=_receipts_text(state.get("receipts") or []),
        # 能力清单与 audience 同一角色源（20260921）：两处口径不同会出现
        # "管理员身份 + 清单里没有管理能力"的自相矛盾 prompt
        page_ctx=_page_ctx(state["messages"], role),
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
    issue = _claim_issue(reply, plan["skill"], plan, bool(frames),
                         _has_exec_memory(msgs), _exec_memory_has_search(msgs),
                         has_popup=bool(state.get("pending_confirm")))
    if issue:
        i_name, i_text, i_clause = issue
        return _fallback_result(i_name, i_text, plan, len(frames), i_clause)
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
            # 兜底文案按**原因码**分（20260921）：同意闸/目标有据这两族错误帧说的
            # 是"还没动手"（等确认 / 不知道改哪一篇），套通用"执行出错了"既与事实
            # 不符、又把用户引向"再试一次"（20260920 §5.2 缺口③ 的同一个根因）。
            clause5a = _claim_clause(reply, _COMPLETION_CLAIM_RE, _WRITE_CONTENT_CLAIM_RE)
            err_text = "\n".join(str(getattr(f, "content", "")) for f in err_frames)
            if authz.consent_error_reason(err_text):
                logger.info("[gate] 写操作未获同意却声称已完成 → fallback(consent)")
                return _fallback_result("err_frame_claim_consent", _FALLBACK_CONSENT,
                                        plan, len(frames), clause5a)
            if A.target_error_reason(err_text):
                logger.info("[gate] 写操作目标无据却声称已完成 → fallback(unknown_target)")
                return _fallback_result("err_frame_claim_target", _FALLBACK_UNKNOWN_TARGET,
                                        plan, len(frames), clause5a)
            logger.info("[gate] 工具帧 __ERROR__ 但回复含完成式声称 → fallback")
            return _fallback_result("err_frame_claim", _FALLBACK_ERR_CLAIM, plan, len(frames),
                                    clause5a)
    # 5b. 确认式导航（NAVIGATE: 帧、无 AUTO_NAVIGATE:）却回复到达声称 →
    #     页面实际未跳转（前端等确认）
    if plan["skill"] == "navigate" and "NAVIGATE:" in tool_text and "AUTO_NAVIGATE:" not in tool_text:
        if _NAV_ARRIVAL_RE.search(reply):
            logger.info("[gate] NAVIGATE 确认帧 + 到达声称 → fallback")
            return _fallback_result("nav_pending_claim", _FALLBACK_NAV_PENDING, plan,
                                    len(frames), _claim_clause(reply, _NAV_ARRIVAL_RE))
    # 5c. 具名工具声称（20260913 C 项）：有帧 ≠ 帧里有那个工具——回复第一人称
    #     完成式点名"我调用了 X"而 X 本轮没执行（越权被剥/被跳过）= 编造调用
    #     （15:51 实证句："这次我用专门的社交链接查询工具（get_social_links）调了一次"）
    executed_names = {str(getattr(m, "name", "") or "") for m in frames}
    # frame_text 传入 = 开启"复述工具自己说的话"豁免（20260921，见 _phantom_tool_claim_span）
    phantom = _phantom_tool_claim_span(reply, executed_names, _has_exec_memory(msgs),
                                       tool_text)
    if phantom:
        logger.info("[gate] 具名工具声称无帧支撑：%s（本轮执行=%s）｜子句=%s → fallback",
                    phantom[0], "、".join(sorted(n for n in executed_names if n)) or "无",
                    _clip_clause(phantom[1]))
        record("gate", "phantom_tool_claim", tool=phantom[0],
               clause=_clip_clause(phantom[1]),
               executed=sorted(n for n in executed_names if n))
        # 文案用**有帧轮**那个变体：这条判据只在真有执行的轮才可能命中（_phantom_tool_claim_span
        # 在 `not executed` 时直接返回 None），_FALLBACK_CLAIM 的"没有任何工具执行"必然为假。
        return _fallback_result("phantom_tool_claim", _FALLBACK_PHANTOM_CLAIM,
                                plan, len(frames))

    # 5d. 站内检索声称 vs 本轮内容类帧（20260919 gate 洞②的混合轮形态）：回复说
    #     "我检索了一圈/把站内翻了一遍/用 rag_search 搜了一遍"，而本轮**一个内容类
    #     工具都没跑**（只跑了导航/特效/设备这类动作工具）→ 检索声称无据。5c 只管
    #     点名工具，泛指检索声称归这里。
    if not (executed_names & _CONTENT_TOOLS):
        own5d = _strip_quoted_spans(reply)
        clause5d = _site_search_claim_clause(own5d, _has_exec_memory(msgs))
        if clause5d:
            logger.info("[gate] 站内检索声称但本轮无内容类工具帧（执行=%s）｜子句=%s → fallback",
                        "、".join(sorted(n for n in executed_names if n)) or "无",
                        _clip_clause(clause5d))
            record("gate", "phantom_search_claim", clause=_clip_clause(clause5d),
                   executed=sorted(n for n in executed_names if n))
            # 有帧轮变体（同 5c 的理由：本分支位于 `if not frames: return` 之后）
            return _fallback_result("phantom_search_claim", _FALLBACK_SEARCH_CLAIM_FRAMED,
                                    plan, len(frames))
        # 5f. 站内"没有"结论 vs 本轮内容类帧（洞④的混合轮形态，20260921）：本轮只跑了
        #     动作类工具（导航/特效/设备），回复却对站内内容下"没有"的结论 → 无依据。
        clause5f = _site_absence_claim_clause(own5d, _exec_memory_has_search(msgs))
        if clause5f:
            logger.info("[gate] 站内『没有』结论但本轮无内容类工具帧（执行=%s）｜子句=%s → fallback",
                        "、".join(sorted(n for n in executed_names if n)) or "无",
                        _clip_clause(clause5f))
            record("gate", "site_absence_claim", clause=_clip_clause(clause5f),
                   executed=sorted(n for n in executed_names if n))
            return _fallback_result("site_absence_claim", _FALLBACK_SITE_ABSENCE,
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


def route_after_execute(state: AgentState) -> Literal["planner", "reflector", "end"]:
    """execute 执行完的下一站（20260904 checker 驱动路由）：
      - pending_confirm（20260921）→ **end**：本轮只弹了个确认框，什么都没执行。
        绝不能去 model——narrator 面对"零工具帧 + 一条待确认的写"最可能的输出
        就是"我已经帮您建好啦"（那正是 gate 一直在打的地鼠）。图到此为止，
        弹窗那一轮的回复文本由 execute 侧确定性给出（confirm_text）。
      - 本轮无受阻项 → planner（正常多轮循环：看工具返回再决策，现状不变）
      - 有受阻项但都是首现（planner rule5 的合法改参重试空间，零新增 LLM）→
        planner 按错误修正重试
      - blocked_repeat（受阻 spec 此前已受阻过 = 首轮重试已败/依赖链断）→
        reflector 复盘（≤2 次 LLM），不再让 planner 盲试第三遍
    """
    if state.get("pending_confirm"):
        return "end"
    # 确认轮执行成功 → **直去 narrator**（20260921）：这一轮不存在"再规划一次"
    # 的任何理由（清单是签过名的），多回一趟 planner 只是多烧一次 LLM 决策、
    # 多一次让模型"重新理解"的机会。受阻则照常回 planner（上面的 rounds 分支
    # 会把第二次进入转成收尾，不重发清单）。
    if state.get("confirm_grant") and not state.get("blocked"):
        return "model"
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

# 路由表（**路由函数返回的每个标签都必须在这里出现**）：langgraph 的
# add_conditional_edges 拿到映射表里没有的返回值时抛 KeyError，而这一步发生在
# **节点已经执行完**之后——写操作已经生效、回执已经落库，流却在收尾前炸掉，
# 前端只看到一行 `'model'` 这样的报错。
# 20260921 22:37 生产实证：确认轮（confirm_grant → 写成功 → 直去 narrator）加进来
# 时漏了 execute 这一侧的 "model" 映射，于是**每一次"点确定"都以报错收场**
# （标签/状态其实改成了，用户看到的是错误）。test_confirm.py ⑦ 用假工具 + 假 LLM
# 把整条确认轮跑一遍当回归锁（含"路由标签 ⊆ 映射表"的全扫）。
PLANNER_ROUTES = {"execute": "execute", "model": "model"}
EXECUTE_ROUTES = {"planner": "planner", "reflector": "reflector",
                  "end": END, "model": "model"}
REFLECTOR_ROUTES = {"planner": "planner", "model": "model"}


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
    g.add_conditional_edges("planner", route_after_planner, PLANNER_ROUTES)
    g.add_conditional_edges("execute", route_after_execute, EXECUTE_ROUTES)
    g.add_conditional_edges("reflector", route_after_reflector, REFLECTOR_ROUTES)
    g.add_edge("model", "gate")
    g.add_edge("gate", END)

    return g.compile()


def graph_input(messages: list, confirm_grant: dict | None = None) -> dict:
    """图输入构造：state 形状归本模块管，调用方（server.py）不手写字段。

    planner 节点会立刻写入 plan/plan_rounds/done，这里给空初值只为了让输入
    形状完整、可读。

    `confirm_grant`（20260921）：隐藏确认请求的**已验签 payload**（server.py 侧
    验签，验不过根本不会走到这里）。它由 planner 的确定性短路径消费，并在
    execute 里放行"同意闸"与"目标有据"两门——用户点的那一下确定就是这两门的凭据。
    """
    return {"messages": messages, "plan": "", "plan_rounds": 0, "done": False,
            "executed": [], "receipts": [], "blocked": [], "blocked_seen": [],
            "blocked_repeat": False, "reflect_rounds": 0, "issues": "",
            "reflect_end": False, "tool_data": [], "fallback_text": "",
            "pending_confirm": None, "confirm_text": "",
            "confirm_grant": confirm_grant}
