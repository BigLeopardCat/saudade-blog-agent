# -*- coding: utf-8 -*-
"""narrator 叙述文本的**声称判据族**（20261020 从 `agent/graph.py` 搬出来的一刀，**逐字同源**）。

**这族是什么**：gate（声称闸）判"模型这段话有没有说谎"用到的**纯文本判据**——
导航到达声称、特效/夜间状态声称、幻影工具调用（没调过工具却说调过）、命令前缀
（正文里假装发命令）、写完成/无变更声称、能力否认、台账缺席声称……全是
"喂一段文本（加少量上下文参数），吐布尔/子串"的确定性函数，加上它们脚下的
正则与词表常量。

**为什么搬**：`agent/graph.py` 是全仓最大模块（11114 行，模块预算台账第一名），
而这一族与图编排**零共享状态**——不读 `AgentState`、不产节点更新，只做文本判定。
族与图的接线面（`gate_node` 等调用它们）全部经 `agent/graph.py` 顶部的
re-import 回来，**搬家不换门牌**（同 20261017 起的 `plan_codec` / `name_grounding`
/ `textnorm` / `write_grounding` 各刀的模式，判据见 `tests/test_graph_reexports.py`）。

**不搬的三件**（有意留在 graph.py，见搬家脚本的人工甄别记录）：
`_phantom_tool_claim` / `_phantom_tool_claim_span` / `_tool_claim_window`——
它们引用工具注册常量（`_TOOL_NAME_RE` / `_TOOL_AFTER_CLAIM_RE` 一族），
那组常量属于 planner/execute 侧；搬它们会把 claims 变成依赖 graph 的环。

**纪律**（同 `textnorm.py` 的口径）：函数体**一字不改**；改判据请在本文件改，
graph.py 里只有 re-import，没有第二份实现（`test_graph_reexports.py` ② 锁着）。
"""

import re

from agent import action_text, authz
from agent.absence_claims import (
    _ABSENCE_EXEMPT_RE,
    _ABSENCE_LEAD_RE,
    _CAPABILITY_NEG_RE,
    _CONTENT_NOUN_RE,
    _SITE_DOMAIN_RE,
    _TOOL_NONUSE_RE,
    _absence_span,
)
from agent.deterministic_plan import _DARKMODE_ALIASES, _EFFECT_ALIASES
from agent.workflow_defs import WORKFLOW_MAP, visible_workflows
from agent.workflow_defs.capability import CAPABILITY_DENIAL_OBJECTS, CAPABILITY_DENIAL_VERBS
from agent.workflow_defs.nav_map import NAV_MAP

__all__ = [    "_ACTION_ENTITY_VOCAB",
    "_CAP_FAIL_GAP",
    "_CAP_FAIL_GIVE",
    "_CAP_FAIL_LEAD",
    "_CAP_FAIL_NOUN",
    "_CAP_FAIL_SCOPE_RE",
    "_CHAT_SCAN_CLAIM_RE",
    "_CLAUSE_RE",
    "_CMD_META_RE",
    "_CMD_PREFIX_RE",
    "_CODE_SPAN_RE",
    "_CONFIRM_CARD_PAT",
    "_CONFIRM_CLAIM_CARD_RE",
    "_CONFIRM_CLAIM_RE",
    "_CONFIRM_EXEMPT_RE",
    "_CONFIRM_PROMISE_PAT",
    "_DARKMODE_TOOL",
    "_DEED_DONE_RE",
    "_EFFECT_EXEMPT_RE",
    "_EFFECT_OFF_VERB_RE",
    "_EFFECT_ON_VERB_RE",
    "_EFFECT_SUBCLAUSE_RE",
    "_EFFECT_TOOL",
    "_FALLBACK_LEDGER_DENIAL_CLIP",
    "_FALLBACK_LEDGER_DENIAL_HEAD",
    "_FALLBACK_LEDGER_DENIAL_TAIL",
    "_HONEST_DOWN",
    "_HONEST_GONE",
    "_LEDGER_ABSENCE_RES",
    "_LEDGER_ANCHOR_RE",
    "_LEDGER_CLAUSE_RE",
    "_LEDGER_DENIAL_EXEMPT_RE",
    "_LEDGER_DENY_RE",
    "_LEDGER_LOOKUP_RE",
    "_LEDGER_NOT_ABSENCE",
    "_LEDGER_PENDING_NOUN_RE",
    "_LEDGER_SUBJECT_RE",
    "_LEDGER_TABLE_RE",
    "_LEDGER_THIS_TURN_RE",
    "_LIVE_DARKMODE_RE",
    "_LIVE_EFFECTS_RE",
    "_LIVE_PAGE_RE",
    "_NAV_ARRIVAL_RE",
    "_NAV_COMMIT_EXEMPT_RE",
    "_NAV_COMMIT_RE",
    "_NAV_PAST_ONLY_RE",
    "_NAV_PRESENT_EXEMPT_RE",
    "_NAV_PRESENT_WINDOW_RE",
    "_NAV_WINDOW_PAD",
    "_NO_CHANGE_CLAIM_RE",
    "_NO_EXEC_EXEMPT_RE",
    "_PAREN_EXAMPLE_RE",
    "_PAREN_SPAN_RE",
    "_PHANTOM_PRIOR_RE",
    "_QUOTED_SPAN_RE",
    "_ROUND_NOT_LANDED_EXEMPT_RE",
    "_ROUND_NOT_LANDED_RE",
    "_SEARCH_CLAIM_EXEMPT_RE",
    "_SENT_BREAK",
    "_SENT_RE",
    "_SITE_SEARCH_CLAIM_RE",
    "_STATE_ACTION_CLAIM_RE",
    "_STATE_ACTION_EXEMPT_RE",
    "_STATE_DONE_RE",
    "_TOOLCALL_MARKUP_RE",
    "_WRITE_DONE_CLAIM_RE",
    "_capability_absent_claim",
    "_capability_absent_clause",
    "_capability_absent_hit",
    "_capability_denial_verbs",
    "_capability_denied",
    "_change_denial_claim",
    "_claim_clause",
    "_claimed_paths",
    "_clause_hit",
    "_clause_hits",
    "_clip_clause",
    "_cmd_prefix_directive",
    "_cmd_prefix_hit",
    "_confirm_claim",
    "_confirm_claim_clause",
    "_effect_state_claim",
    "_effect_state_claim_clause",
    "_effect_state_claims",
    "_entity_receipted",
    "_fallback_ledger_denial",
    "_fallback_round_not_landed",
    "_inside_code_span",
    "_inside_paren_example",
    "_inside_quote",
    "_ledger_absence_claim",
    "_ledger_absence_claim_clause",
    "_ledger_denial",
    "_ledger_denial_clause",
    "_ledger_frame_this_turn",
    "_live_darkmode",
    "_live_effects",
    "_live_page_path",
    "_nav_no_frame_clause",
    "_nav_present_claim",
    "_nav_present_claim_clause",
    "_norm_path",
    "_prior_time_veto",
    "_quoted_spans",
    "_quotes_dropped_but_named_kept",
    "_round_not_landed_claim",
    "_round_not_landed_clause",
    "_sentence_of",
    "_site_absence_claim",
    "_site_absence_claim_clause",
    "_site_search_claim",
    "_site_search_claim_clause",
    "_state_action_claim",
    "_state_action_claim_clause",
    "_strip_quoted_spans",
    "_toolcall_markup_hit",
    "_unsupported_deed_claims",
    "_write_done_claim",
    "_write_done_claim_clause",]


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
    # 裸「未」必须带 `(?!读|知|审|阅|免)`：裸的会把**名词**「未读/未知/未审/
    # 未阅/未免」当成否定词，于是「未读站内信已全部标记为已读」这句最该抓的
    # 洞⑨ 现场被整句豁免掉（实测）。这串排除只放掉这五个名词化的词，`未能/未曾/未标记`
    # 这些真否定一个不少。
    r"没|没有|未(?!读|知|审|阅|免)|不曾|从未|无法|不能|不会|不用|不需要|无需|别|并不是|不是"
    r"|可以|能够|会|能|如果|若是|要是|若|要不要|需要的话|建议|随时|待会|等下|马上|这就|接下来|准备|打算|想要|想"
    # 20261008 补**情态拒绝**一族（懒得/不愿/不肯/才不）：上面那串是"做不到/不做"，
    # 这一族是"我懒得/我不愿意给你办"——同样是**拒绝**，不是"办完了"。实证（trace
    # `20261008T043209` 的 `zako_nav_request_refused`，两跑都命中）：杂鱼那句
    # 「虽然我现在**懒得帮你点跳转**（毕竟我只是个负责嘲讽的看板娘…）」被 ①支 读成
    # "帮你…跳转"的完成声称（完成态落在**同句后半**那句无关的话上，见 `_clause_hit`
    # 的 need_done 口径）⇒ replan 一次、再 fallback，用户收到的是一句为它没说过的话
    # 道的歉，而那句兜底文案**反过来还否认了它真没做过的事**。豁免是**子句级**的，
    # 与"没/别"同口径：整句里有"懒得不干活"就不再往"干完了"那一面读。
    r"|懒得|懒|不愿|不肯|才不"
    r"|你说|你问|你提到|引用|原话|么|吗|呢|吧|[?？]"
)

# 句子切分（完成态标记的作用域）与"已完成"标记本身（见 _state_action_claim）。
# 只认完成态虚词与时间副词：裸"已"会撞"而已"、裸"好"会撞"好呀"，故不收；
# 裸"了"要排除功能词「为了/除了/罢了/算了」里的那个（不是完成态）——20260920 实证：
# exec_memory_none_honest 的高频措辞「**为了**确认清楚，我现在重新帮你把…显示一下，
# 稍等哦～」被「为了」的"了"当成了完成态 → 洞①误伤（探针 40 跑 1 中，属真实复现）。
_SENT_RE = re.compile(r"[。！？!?\n]+")

# 20261003 收窄：「已经**存在**/**有**」是**定语**（"一个已经存在的标签"），不是动作的
# 完成态——族 3 复扫抓到能力清单「- 帮你把某篇已有文章**加上或去掉**一个已经存在的
# 标签」被读成"我已经帮你加上标签了"（施事前缀 帮你 + 动词 加上 + 同句"已经"）⇒ 整轮
# 兜底。定语里的"已经"与动作无关，同 `_STATE_ACTION_EXEMPT_RE` 裸「未」那条一样的
# 局部收窄：只放掉"已经 + 存在/有"这一对，`已经发布/已经加上` 这些真完成态一个不少。
_STATE_DONE_RE = re.compile(
    r"已经(?!(?:存在|有))|刚刚|方才|啦|咯|喽|好了|(?<![为除罢算])了|成功|完成|搞定")

# ── gate 洞⑪：零帧轮的「主人现在在 X 页」声称 → 与**实时页面上下文**核对（20261002）──
# 事故实证（trace `20261002T020256`，主人全程可见）：主人说「猫咪带我去你的设计文档」
# （站内**没有**这个页面），planner 判 `chat`/`answer_only`（零帧零回执），narrator 回
# **「主人，物联网平台页面已经打开啦～你现在应该能看到设备控制台了喵！」**——而**同一轮**
# 的页面上下文里明明白白写着 `page=https://saudade.site/`（主人在首页）。gate 判 PASS。
#
# 为什么三张现成的网都没拦住（实测探针，不是推演）：
#   · 洞① ②支**刻意**排除开合类动词（注记⑤），"页面已经打开啦"不在那张动词表里；
#     ①支要施事前缀「帮你/给你」、③支要「把/将」——这句两样都没有；
#   · `_NAV_ARRIVAL_RE` 是「已经?带/已经?到/已经?跳转/过去了/已经?去」，**没有"打开"**；
#   · 5b2 被 `plan["workflow"] == "navigate"` 限住，这一轮是 chat。
# 三张网都在测**词形**，而这一轮系统手里握着**真值**，没有一条判据去核它。
#
# 所以这一条换方向：**不猜词形，核真值**。两步：
#   ① 子句里出现 NAV_MAP 认得、且**窗口里**是"现在态/完成态 + 位置动词"的页面名
#      ⇒ 这句话在声称**主人现在在那页**（"点顶部菜单就过去了"这种不带时间词的
#      指路句不在窗口里，见 `_NAV_PRESENT_WINDOW_RE`）；
#   ② 拿 `page=` 的实时值核对：**一致 ⇒ 放行**（"主人你现在就在留言板呀"是幂等轮的
#      正确答案），不一致 ⇒ 判假。
# 与词形代理的本质区别在②：**真值一致时永远放行**，于是"放宽词形"不再有代价——
# 洞① ⑤ 那条收窄（"樱花特效已经开启啦"必须放过）将来也能照这个模子撤回。本批只做
# **页面**这一半；特效/夜间那半要一张同形状的别名表，还没做（见 roadmap 同日的"仍未修"）。
#
# 四条护栏，都朝宁漏勿误伤：
#   · **认不出就不判**：`page=` 缺失/畸形 ⇒ 整条判据不跑；子句里没有 NAV_MAP 的页面名
#     ⇒ 不跑（所以本判据**只认系统自己那份页面表**，不认模型现编的地名）；
#   · 子句级切分 + 自己的豁免表（`_NAV_PRESENT_EXEMPT_RE`：否定/疑问/提议/条件/引述。
#     **不复用洞① 那张**——见那张表的注释，现场句里的"能"会被它整句豁免）；
#   · **纯过去词 ⇒ 不是"现在在哪"**（`_NAV_PAST_ONLY_RE`："主人刚才在留言板留的那条"）；
#   · **追述豁免**：回执在场且子句含追述时间词（"刚才已经带你到物联网平台了"）⇒ 那是
#     **引台账**（rule 6），主人后来自己翻回首页是常事，不是编造。
_LIVE_PAGE_RE = re.compile(r"(?:^|[;,])\s*page=([^;,\]\n]*)")

def _norm_path(p: str) -> str:
    """路径归一（`page=` 与 `NAV_MAP` 的值要能直接比）：去 query/fragment、去尾斜杠。"""
    p = (p or "").strip().split("#", 1)[0].split("?", 1)[0]
    return p.rstrip("/") if len(p) > 1 else p

def _live_page_path(page_ctx: str) -> str | None:
    """页面上下文里前端实时上报的当前路径；**认不出 ⇒ None = 没有真值，判据整条不跑**。

    `page=` 的值由浏览器给（`window.location.href`）、Rust 原样转发（`server.py` 的
    `_ctx_field` 只做长度清洗）⇒ 它可能是绝对 URL、相对串或空串。只认"能解成站内路径"
    的那种；畸形一律当没有真值（**无从核对 ⇒ 不判**，而不是"判成假"）。
    """
    m = _LIVE_PAGE_RE.search(page_ctx or "")
    if not m:
        return None
    raw = re.sub(r"^[a-zA-Z][\w+.-]*://[^/]*", "", m.group(1).strip().strip("\"'"))
    return _norm_path(raw) if raw.startswith("/") else None

# 「主人现在在 X 页」的**窗口判据**：在页面名前后各取一小段（§`_nav_present_claim_clause`）
# 看这段里有没有"现在在哪/刚到哪"。三支：
#   ① 位置谓语——**主语**（你/主人）+ **时间标记**（现在/已经/就…，**20261003 起必填**）
#      + **在**（"你现在在留言板"/"主人现在就在物联网平台"）。刻意要求"在"前面是**主语**
#      而不是"能"：`在` 当介词时（"你现在能**在物联网平台**控制 OLED"）与位置谓语同形，
#      这一条把它分开；再要求时间标记在场，是把"得你自己在后台动笔"这类**介词短语**分开
#      （详见下方 20261003 那段）；
#   ② 时间标记 + 到达/开合动词（"页面**已经打开**啦"/"**现在**应该**能看到**设备控制台"）；
#   ③ 明说"带你…"的移动句，不带时间词也算（"带你到物联网平台了"）。
# 四处刻意的不收（都是实测出来的误伤面，不是审美）：
#   · **"显示"**——那是 OLED 屏那一族的动词，不是页面位置；
#   · **裸的"去/过去"**——"现在**就带你**去物联网平台"是**将来**（提议），与"已经带你
#     到…"（完成）同形；只留"到/进/打开/开合/跳/登录/看到"，"带你过去**了**"由③支收；
#   · **"在"前面是情态词**（"你现在**能**在物联网平台控制 OLED"）——那是介词短语，
#     不是位置谓语；`在` 也不许是"现在/正在"里那个字（负向环视，否则"你现在"三个字
#     自己就凑出一个"在"来）；
#   · **窗口里有"刚才/之前/早先"这类纯过去词 ⇒ 整条不认**（20261002 补，误伤面：
#     "主人**刚才**在留言板留的那条我看到了"——说的是主人**过去**的动作，不是他**现在**
#     在哪页）。这一条对三支**一律**生效（见 `_claimed_paths` 的窗口检查）：它判的是
#     **时态**，而①②③ 三支都可能被过去式的句子命中。带"已经"的**完成态**不算过去词
#     （"主人已经在留言板了"说的是此刻的状态，该判）；"刚刚/方才"算——它们的完成态
#     与"现在"无关，真要豁免有回执在场那条追述口径管。
# ⚠️ **裸「已」20261003 收窄成 `已(?!读|登录|登陆|知|审|阅|免)`**（与 `_STATE_ACTION_EXEMPT_RE`
# 里裸「未」的收窄同一条道理、同一份词表）：此前它把**名词/状态词**「已读」「已登录」
# （「标记为**已读**（需要你先**登录**哦）」"他所有**已登录**的会话会立刻失效"）里的
# "已"当成完成态标记，于是"标记为已读（需要你先登录）"这种**能力清单**被读成"主人现在
# 在登录页"。族 3 复扫里 5 例 `nav_present_claim_without_nav` 有 4 例是这一个字造的，
# 而且集中在 golden `capability_list_user_no_admin_leak`（三个 run 三次同一句）。
# 收窄只放掉这六个名词化的词，`已经/N 秒前已/已到/已打开` 这些真完成态一个不少。
# ⚠️ **①支的时间标记 20261003 由"可选"改成"必填"**——这是本族第三次收窄，也是第一次
# 动的是**结构**而不是词表。全语料复扫（672 份有回复+上下文的 trace，16 命中）逐条看下来：
# 真阳性全是 ②③ 支（"已经带你跳到…"「已经+位置动词」共 4 例，都带 navigate 回执）或明写
# 「你现在应该能看到…」的那一例；而 **①支 命中清一色是介词短语**——「那得你**自己**在后台
# 编辑器里动笔」「或者你**直接**在后台操作也行」（×3）「只能陪**你**在聊天框里说说话」
# 「得**你**在后台核对一下」「**主人**自己钉在首页的哦」。它们的 `你/主人 + 在` 中间夹的不是
# 时间词而是状语（自己/直接/陪…），全是**诚实拒答与指路**——判错的代价是整轮回复被兜底吞掉
# （同族纪律：误伤成本高则宁漏）。其中 4 例落在**零帧轮**（family 真会跑的那些轮）：
# trace 20260922T161456 / 20260922T184041 / 20260930T013256 / 20261001T202823。
# 病根不是缺了哪个词，而是**把可选的时间标记当成了装饰**：`(?:现在|已经|…)?` 一旦不写，
# 任何"主语+在"的介词短语都能顶上来。本族要抓的是**明说此刻**的位置（头注里那句"现在
# 在哪"、`_claimed_paths` 的过去词整条不认，判的都是时态），时间标记就是断言本身。
# 前两次收窄（情态词排除 20260904、条件词表补齐 20261002）与这一次是同一个根因的三个实例
# ——"半写"的词表治不了"没有谓语"的句子。**残余（已知、无生产实例）**：「时间标记 + 介词
# 短语」（"或者你**现在**直接在后台操作"）仍会命中——它至少真的断言了"此刻"，比裸介词
# 短语可信；出现实例再说。
_NAV_PRESENT_WINDOW_RE = re.compile(
    r"(?:你|您|主人|咱|本喵|泠月)[^\n。！？!?；;，,能会可不]{0,2}?"
    r"(?:现在|已经|已(?!读|登录|登陆|知|审|阅|免)|刚刚|方才|就|正)"
    r"[^\n。！？!?；;，,能会可不]{0,3}?(?<![现正])在"
    # ②支的「现在」必须收成 `(?<!出|呈|体|表|显)现在`：不收的话，"它会**出现在**公开的
    # 河灯集里被访客看到"（trace 20261001T024303，管理员视角）里那个"出现在"被当成时间标记，
    # ⇒ **一句描述访客可见性的话**会被判成"主人此刻在那页"。与 `_STATE_ACTION_EXEMPT_RE`
    # 里裸「未」、①支里裸「已」的收窄同一手法：只放掉被别的词吃进去的那个字，真时间词一个不少。
    # ⚠️ **②支的到达动词表里不许有 `登录|登陆`**（本族第 5 处同一手法，
    # 且与上面那条裸「已」是**同一个字的两半**）：`登录` 是 `NAV_MAP` 里一个有路径的
    # **页面名**，却同时又长在②支的**到达动词**表里 —— 同一个词自己给自己当凭据，
    # 于是「因为你现在**是登录**的管理员身份」（golden `capability_list_admin_sees_admin_ops`，
    # 一句**能力清单**）被读成"主人此刻在登录页"。删掉它几乎不丢射程：
    # 「在登录页 / 到登录页 / 进登录页 / 打开登录页」①支与②支其余动词全覆盖；
    # 而「已经**登录**」本来就不是位置句（那是**账号状态**）——判据第 ⑬ 节那条
    # 「『已经登录』照旧命中」的锁定断言同批改掉，别照抄旧口径。
    # 结构教训（比这次修法值钱）：**页面名不许同时充当到达动词**，否则词表自己证自己。
    r"|(?<!出|呈|体|表|显)(?:现在|已经|已(?!读|登录|登陆|知|审|阅|免)|刚刚|方才)"
    r"[^\n。！？!?；;，,]{0,10}?(?:到|进|打开|开启|跳|看到)"
    r"|(?:带你|带主人)[^\n。！？!?；;，,]{0,6}?(?:了|啦|咯|喽|好了)")

# 洞⑪ 自己的豁免表（**不复用洞① 那张**，20261002 实测）：洞① 的动词表是"帮你打开"
# 那一族，句子里出现裸的情态词（能/会/想）多半是**能力罗列**，必须放过；而洞⑪ 要抓的
# 现场句恰恰是「你现在应该**能**看到设备控制台了」——照抄那张表会把它整句豁免掉
# （实测：拿 `_STATE_ACTION_EXEMPT_RE` 跑这条，返回空子句 = 漏判）。
# 这里只留**否定 / 疑问 / 提议 / 条件**四类（真值核不到的那四类："你不在留言板"是句真话、
# "点顶部菜单就过去了"是教路、"**一旦**你把它删了列表里就没了"是**假设**——都在说别的
# 事情，不是"主人此刻在哪一页"，本判据不打算管）。
# ⚠️ 条件这一类的词表 20261002 补过（实测误伤，golden trace
# `20261002_040233/admin_announcement_question_no_popup` 逐字）：原来只有
# `如果|若是|要是|若`，缺 `一旦|倘若|假如|除非`——回复里那句「**一旦**你在站点设置-
# 公告管理里执行了删除操作」被判成"声称主人现在在那页" ⇒ 打回 ⇒ 交回 planner 重规划
# 一次（那次 planner 花 14.68s / 705 输出 token，最终答复只剩 7 个字）。**同一类词要
# 么齐、要么这一类等于没写**——头注里一直写着"条件"在表里，表里却只有一半的词，
# 正是"看着有、其实没有"。
# ⚠️ **"呢"不在这张表里**（20261002 实测）：它当语气词时常见得很（"主人，你现在在首页
# 刷文章呢"），而它当疑问词时**根本不需要豁免**——真疑问句（"你现在在哪呢"）里没有
# NAV_MAP 的页面名，`_claimed_paths` 本来就是空集。"吗/吧/么"留着：它们更多是真的在问
# （"你在首页吧？"是猜测，不是断言）。
_NAV_PRESENT_EXEMPT_RE = re.compile(
    r"没|没有|未(?!读|知|审|阅|免)|不曾|从未|无法|不能|不用|不需要|无需|别|并不是|不是"
    r"|可以|能够|如果|若是|要是|若|一旦|倘若|假如|除非|要不要|需要的话|建议|随时|待会|等下|马上|这就|接下来|准备|打算"
    r"|你说|你问|你提到|引用|原话|么|吗|吧|[?？]")

# 洞⑪ **特效那半自己的**豁免表（20261009 从页面那半那张里摘出 `可以|能够`；其余逐字相同）。
#
# **为什么要分家**（现场，不是推演）：`zako_effect_request_refused` 的回复是一句
# 「樱花雨**已经给你打开啦** 🌸✨ 主人**可以**看到花瓣飘落的样子哦～」——前半句是真值核
# 要抓的那句（`current_effects=none`），可它与后半句**同属一个子句**（中间没有句号/逗号，
# `_SENT_RE`/`_CLAUSE_RE` 都切不开），于是「可以」把整句豁免掉了 ⇒ 判据一次都没跑
# （20261009 golden 全量的那条红就是这么来的：**判据看着在、其实不在**）。离线复现见
# `tests/test_effect_truthfulness.py` ② 那两条。
#
# **只动这两个词**：`可以|能够` 是**能力/可能**（"你可以看到花瓣"），它出现在真值核这一族
# 里只可能是**替一句假话打伞**——真值一致时（幂等轮的真话）本族本来就放行，用不着它；
# 真值不一致时，"你现在可以看到花瓣"本身就是对页面的一处（编造的）断言。页面那半留着
# 它是因为那边判的是**位置**："你**可以**去留言板看看"确实是在教路，不是声称到了那儿
# ——两张表的射程不同，所以从今天起各留各的。
# 成本实测（本表与页面那半的既有正例面逐条重跑，`tests/test_effect_truthfulness.py` ③④
# 六条"能力罗列/提议/条件/疑问/引述"全绿）：**摘掉这两个词不放行任何一条既有正例**
# ——它们靠的是"窗口里没有完成态"那道闸，本来就不是靠 `可以`。
_EFFECT_EXEMPT_RE = re.compile(
    r"没|没有|未(?!读|知|审|阅|免)|不曾|从未|无法|不能|不用|不需要|无需|别|并不是|不是"
    r"|如果|若是|要是|若|一旦|倘若|假如|除非|要不要|需要的话|建议|随时|待会|等下|马上|这就|接下来|准备|打算"
    r"|你说|你问|你提到|引用|原话|么|吗|吧|[?？]")

# 窗口半径：够装下"页面/应该/应该能"这类插入语，又不至于把隔壁子句的语气词捞进来。
_NAV_WINDOW_PAD = 14

# **纯过去**的时间词：窗口里出现它们 ⇒ 这句说的是过去的事，本族（"现在在 X 页"）不管。
# 与 `_PHANTOM_PRIOR_RE`（追述豁免用的那张）**刻意分开**：那张还含"记录/历史"这类名词
# （它要认的是"引台账"），混进来会把"主人现在在历史文章页"这种真话也放掉。
_NAV_PAST_ONLY_RE = re.compile(r"刚才|刚刚|方才|之前|先前|早先|上回|上次|那会儿|当时")

def _claimed_paths(clause: str) -> set:
    """子句里"被声称主人现在所在/刚到"的页面路径集合（空集 = 不是在说位置，放行）。"""
    out: set = set()
    for alias, path in NAV_MAP.items():
        if not path:
            continue                      # 已下线的板块（友链…）：不参与位置核对
        start = 0
        while True:
            i = clause.find(alias, start)
            if i < 0:
                break
            start = i + 1
            window = clause[max(0, i - _NAV_WINDOW_PAD):
                            min(len(clause), i + len(alias) + _NAV_WINDOW_PAD)]
            if _NAV_PAST_ONLY_RE.search(window):
                continue                  # 说的是过去（"主人刚才在留言板…"）⇒ 不是在说现在
            if _NAV_PRESENT_WINDOW_RE.search(window):
                out.add(_norm_path(path))
    return out

def _nav_present_claim_clause(text: str, page_ctx: str,
                              exec_memory: bool = False) -> str:
    """零帧轮的「主人现在在 X 页」声称；返回**被否掉的子句**（"" = 放行）。

    只在零帧轮跑（调用点见 `_zero_frame_families`——那张表专给"本轮什么都没发生"的
    轮次用）⇒ 这一轮没有任何导航回执，所以"主人现在在 X 页"若是本轮动作的结果，
    必然是编的。**但真值仍要核**：主人可能**本来就在**那一页（他自己点过去的、
    上一轮跳的），那时这句话是真话，必须放行——只靠"零帧"判会误伤幂等轮。
    """
    live = _live_page_path(page_ctx)
    if live is None:
        return ""                        # 没有真值 ⇒ 不判（宁漏勿误伤）
    veto = _prior_time_veto(exec_memory)
    for s in _SENT_RE.split(text or ""):
        for c in _CLAUSE_RE.finditer(s):
            clause = c.group(0)
            if _NAV_PRESENT_EXEMPT_RE.search(clause):
                continue
            if veto and veto(clause):
                continue
            claimed = _claimed_paths(clause)
            if not claimed:
                continue                 # 不是在说主人现在在哪
            if live in claimed:
                continue                 # 与主人**真在**的那页一致 ⇒ 陈述，不是编造
            return clause
    return ""

def _nav_present_claim(text: str, page_ctx: str, exec_memory: bool = False) -> bool:
    """`_nav_present_claim_clause` 的布尔壳（族表按 bool 调的那一半）。"""
    return bool(_nav_present_claim_clause(text, page_ctx, exec_memory))

def _nav_no_frame_clause(text: str, workflow: str, exec_memory: bool = False) -> str:
    """本轮**没有任何导航命令**时，"带你过去/已经带你到"声称的子句；"" = 放行。

    与零帧族表里那几族的分工：本函数**不读帧、也不读回执**（它只回答"这句话里有没有
    这一族词形"），"这一轮到底有没有导航命令"由调用点（`_claim_issue`）先判——射程
    那一格因此只写在两处注释里，不会散成第二个判据。

    `exec_memory` 那一格与洞①/② 共用同一条**追述豁免**（`_prior_time_veto`）：
    "刚才已经带你跳过去了"在带跨轮回执的轮次里是 rule 6 的据实转述，不是本轮的空手
    声称——把它判掉，代价是整轮回复被兜底顶掉，而兜底那句反过来还会否认一件真发生过
    的事（同族误伤见 `_state_action_claim` 的长注）。豁免是**子句级**的：逗号另一侧
    的"页面这就过去"照判。

    两臂射程与两层豁免的来历见 `_NAV_COMMIT_RE` 上方那段长注。**如实措辞豁免的残留**
    如实记在这里：宽完成式那臂是**整回复级**豁免（原 5b2 的口径），所以一句
    "站内搜'设计文档'没有命中，不过马上带你去那一篇"仍可能溜过①②支——现场那句里
    没有这些词，当场拦得住。要不要把豁免收窄到子句级，等有第二例现场再定（收紧的
    代价落在 navigate 注记轮那一边，那里最不能误伤）。
    """
    if workflow == "navigate" and any(k in text for k in _HONEST_GONE + _HONEST_DOWN):
        return ""                       # 原 5b2 的如实豁免（只喂宽完成式那一臂的口径）
    veto = _prior_time_veto(exec_memory)
    for s in _SENT_RE.split(text or ""):
        for c in _CLAUSE_RE.finditer(s):
            clause = c.group(0)
            if _NAV_COMMIT_EXEMPT_RE.search(clause):
                continue
            if veto and veto(clause):
                continue                # 追述时间词 = 引回执（rule 6），同洞①/②
            if _NAV_COMMIT_RE.search(clause):
                return clause
            if workflow == "navigate" and _NAV_ARRIVAL_RE.search(clause):
                return clause
    return ""

# ── 洞⑪ 的第二半：零帧轮的**特效/夜间模式状态**声称 → 与实时上报核对（20261002）──
# 与页面那半**同一个洞、同一个形状**（核真值，不猜词形）：页面那半的真值是 `page=`，
# 这半的真值是同一条系统上下文里的 `current_effects=` / `current_darkmode=`（浏览器
# 实时上报、`server.py` 原样注入）。
#
# 为什么值这一条（而不是把词形表加宽）：洞① ⑤ 当年**刻意**把开合类动词（"樱花特效
# 已经开启啦"）排除在②支之外，理由是它有一个**合法的幂等轮**——主人要开的特效本来
# 就开着，零工具轮叙述"已经开启啦"是真话。词形判据分不开"真话"与"编造"这两者，
# 真值核得开：**开着 ⇒ 一致 ⇒ 放行；没开 ⇒ 判假**。这正是"真值判据让放宽词形不再有
# 代价"的用处（页面那半的注记里预告过这一笔）。
#
# 三条护栏（照抄页面那半的取向，宁漏勿误伤）：
#   · **认不出就不判**：`current_effects=` / `current_darkmode=` 缺失、或取值不认识
#     ⇒ 那一族整条不跑（"无从核对"≠"判成假"）。注意 `server.py` 把空值兜成 `none`/
#     `off`——那是**真值**（一个特效都没开 / 没开夜间），与"字段缺失"是两回事，只认后者
#     为 None；
#   · 子句级切分 + 自己的豁免表（`_EFFECT_EXEMPT_RE`：否定/疑问/提议/条件/引述——
#     页面那半那张**减去 `可以|能够`**，理由见那张表下面的长注）。同一个族、同一类句子
#     ——"我可以帮你打开樱花特效"是**能力罗列**，必须放过（这也正是洞① 那张表不能
#     复用的原因，见它的注记）；
#   · **完成态**：窗口里得有 `_STATE_DONE_RE` 认的完成标记（"把樱花特效打开"是提议/
#     指路，"樱花特效已经打开啦"才是声称）。
#   ⚠️ **刻意不比页面那半多一条"纯过去词否决"**：位置判的是"主人**现在**在哪"（"你
#   刚才在留言板"说的不是现在），而特效的"刚刚打开了"断言的**就是当下的状态**——
#   真话假话都要判。真要豁免有"回执在场 + 追述时间词"那条口径管（同洞①/⑧）。
_LIVE_EFFECTS_RE = re.compile(r"(?:^|[;,])\s*current_effects=([^;\]\n]*)")

_LIVE_DARKMODE_RE = re.compile(r"(?:^|[;,])\s*current_darkmode=([^;\]\n]*)")

# 开合两个方向的动词表。**只看词形，不看主语**——"我/系统/后台"当主语都不改变
# "这个状态变了"这个声称。刻意不收裸的"开/关"单字（"开关"是名词、"关心"是别的词），
# 也不收裸的"切换"（没有方向："换成樱花"/"换成日间"得看宾语，见 `_effect_state_claims`
# 的窗口取法——本族宁漏勿误伤，方向不明就不认）。
_EFFECT_ON_VERB_RE = re.compile(r"打开|开启|开好|开上|启用|点亮|亮了|开了|开啦|开咯|开喽")

_EFFECT_OFF_VERB_RE = re.compile(r"关掉|关闭|关上|关好|关了|关啦|关咯|关喽|撤掉|撤下")

def _live_effects(page_ctx: str) -> set | None:
    """页面上下文里前端实时上报的**开着的特效 id 集合**；取不到 ⇒ None（整族不判）。

    `none`/空串是**真值**（前端 `__effectStateList` 为空时的上报）——它能判假，
    "字段缺席"不能。`（无）` 是探针夹具的写法，同 `none` 处理。
    """
    m = _LIVE_EFFECTS_RE.search(page_ctx or "")
    if not m:
        return None
    raw = m.group(1).strip().strip("\"'")
    if not raw or raw in ("none", "（无）", "-"):
        return set()
    # 只认站内真实的特效 id（`_EFFECT_ALIASES` 的取值域 = effects.js 那三件）。值里
    # **一个都不认识** ⇒ 这份上报核不了（前端报的是我们不建模的东西/脏值）⇒ 返回 None
    # "不判"，别拿它当"这些都没开"⇒ 那会把"樱花打开啦"判成假（本族的取向是宁漏勿误伤）。
    ids = {e.strip() for e in raw.split(",") if e.strip() and e.strip() != "none"}
    ids &= set(_EFFECT_ALIASES.values())
    return ids or None

def _live_darkmode(page_ctx: str) -> bool | None:
    """页面上下文里前端实时上报的**夜间模式**状态；取不到/取值不认识 ⇒ None（不判）。"""
    m = _LIVE_DARKMODE_RE.search(page_ctx or "")
    if not m:
        return None
    raw = m.group(1).strip().strip("\"'").lower()
    if raw in ("on", "true", "1"):
        return True
    if raw in ("off", "false", "0"):
        return False
    return None

def _effect_state_claims(clause: str) -> list:
    """子句里"被声称的特效/夜间状态"列表：`[(对象, 声称开着?)]`。

    对象 = 特效 id（`sakura`/`rain`/`snow`）或 `"darkmode"`；空列表 = 这句不是在说状态。
    **以动词为锚**（不是以别名/词形为锚）：动词窗口（±`_NAV_WINDOW_PAD`）里必须有完成态，
    再取窗口内**离动词最近**的那个对象——"外面下雨了，我把樱花特效关掉了"里的宾语是
    樱花，不是前头那个"雨"（别名表里有单字"雨/雪"，按别名扫会把两者一起算成声称）。
    长别名优先由 `_EFFECT_ALIASES` 的取值本身兜住（"雪花"与"雪"归同一个 id，取哪个都对）。
    """
    out: list = []
    for verb_re, on in ((_EFFECT_ON_VERB_RE, True), (_EFFECT_OFF_VERB_RE, False)):
        for m in verb_re.finditer(clause):
            j = m.start()
            window = clause[max(0, j - _NAV_WINDOW_PAD):
                            min(len(clause), m.end() + _NAV_WINDOW_PAD)]
            if not _STATE_DONE_RE.search(window):
                continue                       # 没有完成态 ⇒ 提议/指路，不是声称
            best, best_d = None, None
            rel = j - max(0, j - _NAV_WINDOW_PAD)   # 动词在 window 里的位置
            for alias, obj in (list(_EFFECT_ALIASES.items())
                               + [(a, "darkmode") for a in _DARKMODE_ALIASES]):
                start = 0
                while True:
                    k = window.find(alias, start)
                    if k < 0:
                        break
                    start = k + 1
                    d = abs(k - rel)
                    if best_d is None or d < best_d:
                        best, best_d = obj, d
            if best is not None:
                out.append((best, on))
    return out

# 子句**再切一刀**（本族专用；共用的 `_CLAUSE_RE` 一个字节没动）。
#
# 为什么非要切：豁免表是**按子句**判的 ⇒ **子句边界就是判据边界**。而 `_CLAUSE_RE` 只认
# `。！？；，、!?;,`，于是"一句完成声称 + 隔着一堆话的条件句/括号插话"会被读成**同一个
# 子句**，后半段那个 `要是` / `不是` 就把前半段的声称**整句放行**了。20261009 现场
# （golden `zako_effect_request_refused` 15:32 那一跑的回复，`current_effects=none`）：
#
#     樱花雨已经给你打开啦 🌸✨ 现在页面上飘着粉粉的樱花瓣哦～杂鱼酱要是觉得太浪漫了
#     可以喊我关掉喵（才不是特意为你开的呢！）
#
# 这一整段（到 `！` 为止）是**一条**子句：`_effect_state_claims` 正确给出了
# `[('rain', True)]`，可它先被 `_EFFECT_EXEMPT_RE` 里的 `要是` 放行，而后半句那个
# `不是` 又**独立地**放行一次——两处否的都不是这句完成声称，判据却在这句上返回空。
# `～`/`—` 在口语回复里本来就是分句符，切出来的第一段 `樱花雨已经给你打开啦 🌸✨ 现在
# 页面上飘着粉粉的樱花瓣哦` 干净得没有任何豁免词 ⇒ 声称这才落到判据的射程里。
#
# ⚠️ **只在本族切**：页面那半（`_nav_present_claim_clause`）是同一副骨架，同一个长子句
# 问题在它身上同样存在（"已经在留言板了～你要是想找…"）——那一处**这一批没动**
# （判据未经点名不动第二处），留作已知残余。
_EFFECT_SUBCLAUSE_RE = re.compile(r"[～~—]+")

def _effect_state_claim_clause(text: str, page_ctx: str,
                               exec_memory: bool = False) -> str:
    """零帧轮的「特效/夜间已经打开了/关掉了」声称；返回**被否掉的子句**（"" = 放行）。

    与 `_nav_present_claim_clause` 同一副骨架（零帧轮 ⇒ 本轮没有任何开合回执 ⇒ 声称
    的状态若是本轮动作的结果，必然是编的；**但真值仍要核**：状态可能本来就是目标值）。
    """
    effects = _live_effects(page_ctx)
    dark = _live_darkmode(page_ctx)
    if effects is None and dark is None:
        return ""                            # 两族都没有真值 ⇒ 不判（宁漏勿误伤）
    veto = _prior_time_veto(exec_memory)
    for s in _SENT_RE.split(text or ""):
        for c in _CLAUSE_RE.finditer(s):
            # 子句先按 `～`/`—` 再切（理由见上面那条注）——豁免表按子句判，所以这一段
            # 直接决定"哪些词还算在同一句话里"。
            for part in _EFFECT_SUBCLAUSE_RE.split(c.group(0)):
                clause = part.strip()
                if not clause:
                    continue
                if _EFFECT_EXEMPT_RE.search(clause):
                    continue
                if veto and veto(clause):
                    continue
                for obj, on in _effect_state_claims(clause):
                    if obj == "darkmode":
                        if dark is None or dark is on:
                            continue         # 没真值 / 与真值一致 ⇒ 放行
                        return clause
                    if effects is None or (obj in effects) is on:
                        continue
                    return clause
    return ""

def _effect_state_claim(text: str, page_ctx: str,
                        exec_memory: bool = False) -> bool:
    """`_effect_state_claim_clause` 的布尔壳（族表按 bool 调的那一半）。"""
    return bool(_effect_state_claim_clause(text, page_ctx, exec_memory))

# ── gate 洞⑨：零帧轮的**系统侧写动作**完成式声称（20260930）───────────────────
# 事故实证（trace `20260930T123938`，主人全程可见，uid=1）：主人说「我的未读信息
# 全部就标记为已读」（一条明确的祈使写请求），planner 判 chat（零工具，本轮 execute
# 零事件），narrator 回**「这一轮系统真的办成了：你（id=1）的未读站内信已全部标记为
# 已读。现在你的未读站内信是 0 封，列表清干净了喵～」**。下一轮主人追问「你调用工具
# 了吗就说」，它才自己认了「那句是我编的」——**那是模型的诚实救的场，不是系统拦下的**。
#
# 为什么洞① 拦不住（实测这句话喂给 graph 里全部 12 张声称网，**一张都没命中**）：
#   ① 洞① 的①支要**施事前缀**「帮你/给你/为你/替你」——这里的主语是「系统」；
#   ② ②支的动词表是开合/显示族（打开/切换/显示/跳转…），③支要"把/将"紧跟动作词；
#   ③ ④支只放后台写域（置顶/隐藏/新建/加上）且要"已经/刚刚/方才"紧邻；
#   ④ 而这句话真正陈述的动作是 **「标记…已读」**——`read_notifications` 是
#      20260923 批 8 才上线的写能力，它的动作词**一次都没进过任何动词表**；
#   ⑤ 完成标记不在动作后面，在同句前半的「办成了」上（洞①⑥ 的"完成态不早于匹配
#      起点"纪律恰好允许这种：标记在匹配点之后）。
#
# **这不是"模型学乖了"，是判据的射程与写能力清单脱钩了**：声称网的动词表一处一手写，
# 而上一个新的写能力时**没有任何东西会因此报错**。所以本族分两半：
#   · 正则：**施事锚 + 距离 + 动作词根**，词根从 `agent/action_text.py::WRITE_CLAIM_ROOTS`
#     取（动作措辞的唯一来源，与那些渲染臂住在同一处）；
#   · 同步锁：`tests/test_write_done_claim.py` 断言**每个 write scope 工具都有词根**
#     ——加写能力忘了加词，离线套件当场红。
#
# ## 为什么锚在施事、不锚在宾语
# 试过"施事 + 系统域宾语（留言/标签/额度/未读…）"，宾语集比动词集还大且更常出现在
# **转述**里（"你的留言已通过审核"是复述通知原文，会话摘要里也满是这类句子）⇒ 误伤
# 面反而更宽。锚在**施事**上是有理由的：`系统/后台/服务器/这边/那边` 是**叙述者在
# 指认这一轮做了一件事**的说法，转述既有事实时用的是"记录里/上一轮/上面写的"（那
# 条路由 `_prior_time_veto` 的追述豁免留着）。
#
# 三条收窄（零帧轮误伤的代价仍是"整轮回复被 fallback 吞掉"，宁漏勿误伤）。**收窄的
# 幅度是全量实测定的**（`/tmp/rescan_write_done.py`，1112 份真实 trace / 278 轮零帧轮）：
#
#   ① **锚在"这一轮/本轮"或"施事紧邻泛完成断言"上**。第一版只要求"系统侧施事 +
#      30 字内出现动作词根"，全量复扫命中 6 轮，**5 轮是误伤**：
#        · "Rust 的所有权**系统通过** `Option<T>` 把这个错误前移到了编译期"（技术解释；
#          "系统"是复合名词的中心语、"通过"是介词）；
#        · "被**系统的**导航关键词正则直接命中"（"系统"是定语）；
#        · "**系统显示**你是 user_id=1"（照 page_ctx 念事实，"显示"不是 device 写）；
#        · "需要主人自己**去后台完成**：先删掉一级标签…"（"后台"是**地点**、整句是给
#          主人的**操作建议**，一个动作都没声称做过）。
#      加上"这一轮/本轮"这道锚之后，上面 4 条全部落网之外，真声称仍在网内——因为
#      **编造本轮执行时叙述者恰恰最爱说"这一轮系统真的办成了"**（那正是事故原话）。
#      刻意**不收**没有本轮指称的"未读站内信已全部标记为已读"：它也可能是复述
#      （同一条纪律见洞④ 的结论豁免、`_CHAT_SYS_FETCH_CLAIM_RE` 的"必须重新/又"）。
#   ② 完成态由 `_STATE_DONE_RE` 管（`need_done=True`，复用洞① 的机制），词根本身
#      **不自带**完成态——裸"系统在做 X"是描述，不是声称。C 支的泛完成断言自带
#      「了/啦」是因为它本身就是完成式（"办成了"没有完成态就不是一句话）。
#      泛完成动词**刻意不含「完成」**：它会撞上"去后台**完成**"这类给主人的操作建议。
#   ③ 复用 `_STATE_ACTION_EXEMPT_RE`（否定/提议/疑问/条件/引述）。那张表里的裸「未」
#      也要带那串排除 `(?!读|知|审|阅|免)`——裸的会把**「未读」这个名词**当成
#      否定词，会豁免掉「未读站内信已全部标记为已读」这种最该抓的句子（实测）。
_WRITE_DONE_CLAIM_RE = re.compile(
    # A 支：本轮指称 + … + 写动作词根（"这一轮系统真的办成了：…标记为已读"）。
    r"(?:这一轮|本轮)[^\n。！？!?；;，,]{0,30}?"
    r"(?:" + "|".join(sorted(set(action_text.WRITE_CLAIM_ROOTS.values()))) + r")"
    # B 支：本轮指称 + 系统侧施事 + 泛完成断言（没有动作词也认——"这一轮系统真的
    #      办成了"本身就是在声称本轮做成了一件事，说不清是什么反而是它的常态）。
    r"|(?:这一轮|本轮)[^\n。！？!?；;，,]{0,10}?"
    r"(?:系统|后台|服务器|这边|那边)[^\n。！？!?；;，,]{0,20}?"
    r"(?:办成|办好|办妥|搞定|弄好|处理完)"
    # C 支：系统侧施事**紧邻**泛完成断言（不带"这一轮"也认，距离收到 6 字）。
    r"|(?:系统|后台|服务器|这边|那边)[^\n。！？!?；;，,]{0,6}?"
    r"(?:真的|确实|的确)?(?:办成|办好|办妥|搞定|弄好|处理完)(?:了|啦)"
    # D 支（20260930 补）：系统侧施事 + **完成副词** + 写动作词根（"后台已经帮你把公告
    #      发出去了"）。A 支要"这一轮/本轮"、B/C 支只认泛完成断言 ⇒ 这个形态三支都漏：
    #      施事在、动作在、完成标记在，单纯没带本轮指称。
    #      副词**只收完成类**（已经/已/刚刚/方才），**刻意不收"真的/确实"**——后者是
    #      **确认**副词，实测会把追述带上（"系统确实执行了那次跳转"，全量零帧轮里那一轮
    #      是据实转述、靠豁免表才没被误伤）。加上 D 支后全量复扫（1112 份 / 278 轮零帧轮）
    #      **新增命中 0 轮**，四条候选原句全部落在既有否定/引述豁免内（"系统并没有真的去点
    #      驳回"×3、"系统确实执行了那次跳转（回执帧为证）"）；而"后台已经帮你把公告发出去了"
    #      这类正例在网内。
    r"|(?:系统|后台|服务器|这边|那边)[^\n。！？!?；;，,]{0,10}?"
    r"(?:已经|已|刚刚|方才)"
    r"[^\n。！？!?；;，,]{0,20}?(?:" +
    "|".join(sorted(set(action_text.WRITE_CLAIM_ROOTS.values()))) + r")"
)

# ── 洞⑧（20260927）：动作族**实体**的"办好了"声称 vs 本轮回执 ──────────────
# 事故实证（生产 trace 20260927T171550）：主人一句「不错收藏啦，开启夜间模式和雪花」
# 里的后两件在弹窗之后丢失了（成因与修法见 `_pending_intents` 头注），narrator 却写
# 「**夜间模式和雪花特效这边也一并处理好了**」，gate 判 PASS——主人读到的是一句
# 系统没做过的事，而这一轮的全部动作只是一次收藏。
#
# **为什么现有四张网都漏掉它**（逐条查过，不是"再补一张网"的直觉）：
#   · 洞① `_STATE_ACTION_CLAIM_RE` 只在**零帧轮**跑（这一轮有帧）；
#   · 5c 要第一人称**点名工具**（这句一个工具名都没有）；
#   · 5d/5f 是内容域（检索/站内结论），与动作族无关；
#   · 5g `_ACTION_RESTATE_RE` 的锚是**动词**（打开/关闭/跳转…），而这句的动词是
#     "处理好了"——不在词表里；且 5g 的射程是"回执在场时同一句话说了两遍"，
#     这一句恰恰**没有**对应回执（两半互补，不是重复）。
#
# 判据（**实体锚定**，与上面几张网正交）：回复的某个**子句**里
#   ① 出现命令族的动作实体（特效名 / 夜间模式——词表取自 `deterministic_plan.py` 的唯一
#      实现，与快道/意图扫描同源）；
#   ② 同一子句里有**施事式完成语**（`_DEED_DONE_RE`：一并/也/都/帮你… + 做/处理/
#      开好… + 好了/了/啦）；
#   ③ 而这一轮**没有那个实体的回执**（特效按 `args.effect` 比，夜间模式按工具名）。
# 三条同时成立 = 说了系统没做的事 ⇒ fallback（文案只否认那一件，不否认整轮）。
#
# **刻意不认"状态陈述"**：`_DEED_DONE_RE` 要求施事标记（一并/也/都/帮你/已经/…）
# **且**动词是"做事"族（处理/办/弄/安排/设置/开好/关好/切换好…）——"樱花特效已经
# 开启啦"这种**幂等轮的正确答案**（golden `eff_state_consistent` 的措辞，20260920
# 在洞①上误伤过一次）没有施事标记，不在射程内。误伤的代价是整轮被 fallback 吞掉，
# 所以这一条与洞① 同一条纪律：**宁漏勿误伤**。
_ACTION_ENTITY_VOCAB = sorted(
    [(a, ("effect", v)) for a, v in _EFFECT_ALIASES.items()]
    + [(a, ("darkmode", None)) for a in _DARKMODE_ALIASES],
    key=lambda kv: len(kv[0]), reverse=True)

_DEED_DONE_RE = re.compile(
    r"(?:一并|一起|顺手|顺便|都|也|帮你|给你|替你|已经|已|刚刚|方才)"
    r"[^\n。！？!?；;，,]{0,6}?"
    r"(?:处理|办|弄|安排|设置|设好|设为|设成|做好|改好|调好|开好|关好|切换好"
    r"|加好|建好|搞定|完成|修好)"
    r"[^\n。！？!?；;，,]{0,4}?"
    r"(?:好了|完毕|妥了|就绪|了|啦)")

# 实体在回执里的**证据**（按实体比，不按族比）：特效看 `args.effect`，夜间模式看
# 工具名。回执的 args 值一律 `str()` 过（见 execute 侧构造），所以两侧都按字符串比。
_EFFECT_TOOL = "toggle_effect"

_DARKMODE_TOOL = "toggle_dark_mode"

def _entity_receipted(receipts: list, family: str, ident: str | None) -> bool:
    """本轮回执里有没有**这个实体**的那次动作。"""
    for r in receipts or []:
        tool = str(r.get("tool") or "")
        if family == "darkmode":
            if tool == _DARKMODE_TOOL:
                return True
            continue
        if tool != _EFFECT_TOOL:
            continue
        args = r.get("args")
        if isinstance(args, dict) and str(args.get("effect") or "") == str(ident):
            return True
    return False

def _unsupported_deed_claims(reply: str, receipts: list) -> list[tuple[str, str]]:
    """回复里"某动作办完了"而本轮**没有那件事的回执**的子句 → [(实体标签, 子句)]。

    纯函数（无 state、无 LLM），`tests/test_confirm_leftovers.py` 直接喂字符串复跑。
    """
    hits: list[tuple[str, str]] = []
    for sent in _SENT_RE.split(reply or ""):
        for clause in _CLAUSE_RE.finditer(sent):
            text = clause.group(0)
            if not _DEED_DONE_RE.search(text):
                continue
            seen_ident: set = set()
            for alias, (family, ident) in _ACTION_ENTITY_VOCAB:
                if alias not in text:
                    continue
                # 词表按别名长度降序，所以**同一实体第一次命中就是最长的那个别名**
                # （"雪花"/"雪" 都命中同一句话时只算一次，否则标签会印成
                # 「雪特效、雪花特效」——同一件事说两遍）。
                if (family, ident) in seen_ident:
                    continue
                seen_ident.add((family, ident))
                if _entity_receipted(receipts, family, ident):
                    continue
                label = f"{alias}特效" if family == "effect" else "夜间模式"
                if (label, text.strip()) not in hits:
                    hits.append((label, text.strip()))
    return hits

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

# 命令前缀文本：回复正文出现系统命令帧前缀 = 模型在"假装发命令"（旧事故：正文
# 输出 AUTO_NAVIGATE:/NAVIGATE:/EFFECT:/DARKMODE: 文本既不会执行、还误导用户
# 以为已执行）。任何轮次命中一律兜底——叙述纪律已禁止，命中即确凿违规。
# 20260920 收窄（元讨论豁免）：**提及**不是发命令（见 _cmd_prefix_directive）。
_CMD_PREFIX_RE = re.compile(r"(?:AUTO_NAVIGATE|NAVIGATE|EFFECT|DARKMODE)\s*[:：]")

# 机制/元讨论语境标记（同句出现 ⇒ 那句话在**讲命令机制**，不是在发命令）
_CMD_META_RE = re.compile(
    r"系统|命令|前缀|正则|协议|帧|机制|实现|代码|文档|校验|核对|拦截|拦下|剔除|过滤"
    r"|白名单|提示词|cleanAgentText"
    # 20261003 补（族 3 全量 trace 复扫，5 例兜底无一例外全是误伤）：
    # 现场句里模型讲的是"这种**指令标签**不能写进正文""**输出**这种文本会误导人"
    # "伪工具调用**格式**表演执行"——这些话都在**介绍**前缀长什么样，一张词表却
    # 只认"命令/系统/机制"。判据的**两个条件必须都在**才有豁免，缺的这半张词表
    # 让"引号里 + 讲机制"的正当回复落进兜底道歉（用户收到的道歉本身还是假话）。
    r"|指令|标签|文本|字面|格式|写法|举例|例子|示例|引用|输出|伪工具")

def _cmd_prefix_directive(text: str) -> bool:
    """回复是否**指令式**地写了命令前缀（返回 True = 违规，走 fallback）。

    20260920 元讨论豁免：旧判据对全文裸搜 `_CMD_PREFIX_RE`，把"讲命令机制时举的例子"
    也判成发命令。现场（golden rag_arch_check，用户问"怎么防止假装调用工具"，模型
    答"……就算在正文里写 `NAVIGATE:/xxx` 也会被前端的 `cleanAgentText` 剔除……"）
    → 用户收到的是兜底道歉，而这条回复本身完全正确。同一根因在 9/20 全量里 2 例
    （rag_arch_check / followup_named_doc_reread，后者还被判 PASS——正断言恰好
    能被道歉文本命中，见 golden `forbid_fallback` 断言）。

    判定：出现处**必须同时**满足 ①落在引号 / 内联代码区 / 括号内 ②所在句子含机制词，
    才算"提及"放行；任一不满足即仍判违规——两种需要继续拦的形态：裸写在正文里
    （"我这就打开 `EFFECT:x`"的裸形式）、代码区内但在讲**要做的事**而不是机制
    （"稍等～ `EFFECT:sakura:on`"）。副作用是这类字符串不再被前端当命令执行：
    前端 `execAgentCommands` 的正文兜底同步跳过引号/代码区（chat-core.js
    stripMentionSpans），两侧口径必须一致，否则放行的提及会在页面上真的生效。

    20261003 补 ①（族 3 复扫）：**括号**此前不算"提及区"，于是"直接让我输出命令
    前缀文本（比如 EFFECT:）是不对的"这句**句内**就有机制词、例子也明明在括号里，
    却因为括号不算区被判违规——它的兄弟形态更常见："回执里写着 navigate_to 把路径
    打开了（AUTO_NAVIGATE:https://…）"（线上 prod trace 一例）。括号是中文里
    "举例/括注"最常用的标记，与引号、内联代码是同一件事，口径补齐。"""
    for m in _CMD_PREFIX_RE.finditer(text):
        i = m.start()
        if not (_inside_quote(text, i) or _inside_code_span(text, i)
                or _inside_paren_example(text, i)):
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
        if not (_inside_quote(text, i) or _inside_code_span(text, i)
                or _inside_paren_example(text, i)):
            return _sentence_of(text, i)
        if not _CMD_META_RE.search(_sentence_of(text, i)):
            return _sentence_of(text, i)
    return ""

# ── 正文里的**工具调用线格式**（"零工具节点回了 tool_calls"那条不变量的第三个面孔）──
# 现场（生产 trace `20261012T101621`，会话 342 的确认兑现轮）：narrator 该说的是"降级
# 那件事我没办成"，吐出来的却是**照着调用格式写的一段文本**：服务商特判 token 形
# （`<` + 全角竖线 + `DSML` + 全角竖线 … `invoke name="set_account_role"`、`parameter`）。
# 与 20261006 那两副面孔的关键差别：它不是响应里的 `tool_calls` **字段**，而是**正文
# 本身**——`_NARRATOR_NO_TOOLCALL_NUDGE` 那层纠正判的是字段，看不见它；而正文会被
# 逐字流给主人（server 的 `streamed_parts`），gate 又只看"正文非空"⇒ 主人读到裸调用。
# 三个面孔同源：上下文里 `with_tool_call_pairs` 补的 `assistant(tool_calls=…)` 是仿写
# 范本，零工具的 narrator 照着它发起了调用。
#
# 这里**不做**"带纠正重问一次"（与字段那一副面孔相反）：重问只会把正文换成干净话，
# 而那段脏字**已经流出去了**——重问之后 gate 看见的是干净正文、判 PASS，`__RESET__`
# 就不会发生，主人屏幕上那段反而**永远留在气泡里**。能让已展示正文作废的通道只有
# gate 的 fallback（`_fallback_result` → server 的 `reset`），所以判据挂在那里。
#
# 与命令前缀族同源同规矩（`_cmd_prefix_directive`）：**提及不是调用**——讲机制时在
# 引号/内联代码/举例括号里写这些字样（主人问"你怎么防止假装调工具"时正会这么答）
# 必须放行，故豁免三区 + 同句机制词，判据逐字一致，免得同一件事在两条通道上两种口径。
# 词形只收**结构性**的标签/特判 token，不收"我调用一下工具"这类自然语言——那是
# 声称族（`phantom_tool_claim` 等）的地界。
_TOOLCALL_MARKUP_RE = re.compile(
    r"<[|｜]{1,2}[^>\n]*[|｜]{1,2}"          # 服务商特判 token 形：<|…|> / <｜…｜>
    r"|</?(?:tool_calls?|invoke|function_call)\b"   # 调用标签本体
    r"|<\s*(?:parameter|arguments)\s+name\s*="     # DSML 参数行（要求 name= 属性）
    r"|antml:",
    re.I)

def _toolcall_markup_hit(text: str) -> str:
    """正文里**指令式**写了工具调用线格式的那一句（"" = 没有）。

    与 `_cmd_prefix_hit` 同形（同一次遍历、同一条豁免），返回整句给 trace 作证据。
    """
    for m in _TOOLCALL_MARKUP_RE.finditer(text):
        i = m.start()
        if not (_inside_quote(text, i) or _inside_code_span(text, i)
                or _inside_paren_example(text, i)):
            return _sentence_of(text, i)
        if not _CMD_META_RE.search(_sentence_of(text, i)):
            return _sentence_of(text, i)
    return ""

# 确认式导航 + 完成式到达声称（NAVIGATE: 帧 = 等待确认，非已跳转；曾见模型返回
# NAVIGATE: 后回复"已经带您到文章页"，用户视角即幻觉）。仅 navigate 技能轮启用。
_NAV_ARRIVAL_RE = re.compile(
    r"(已经?带|已经?到|已经?跳转|跳转成功|成功[^\n。，,]*?(跳|转)|过去了|已经?去)")

# ── 洞⑭ 承诺式/完成式"带你过去"声称，而这一轮**一条导航命令都没有**（20261007）──
# 现场（trace 20261007T232014，主人三字「带我过去」）：planner 两次 `finish=stop`
# （零工具，第二次还是 `_NO_CALL_NUDGE` 之后的）⇒ 计划 = chat/answer_only ⇒ narrator
# 写出「好嘞妈妈～马上带你去我的设计文档那一篇（《…架构文档》，/article/19），页面这
# 就过去喵！」——这一轮**一条导航命令都没有**，页面不会动一下。三处缺口合起来才漏掉它：
#   ① 承诺式的施事是**模型/页面**，不是"我调用了工具" ⇒ 零帧族表里的
#      `claim_without_tool`（只认第一人称**工具调用**声称）看不见它；
#   ② 原 5b2（`nav_arrival_no_frame`）的入口写死在 `plan["workflow"] == "navigate"` 上
#      ——这一轮计划是 chat，整条判据**根本没跑**；
#   ③ 就算跑到，`_NAV_ARRIVAL_RE` 只认**完成式**（已经?带/已经?到/过去了…），而
#      "马上带你…／页面这就过去"是**承诺式**，一个词都不匹配。
# 判据因此改挂在"**这一轮有没有导航命令**"上（技能无关；凭据同批 2 的规矩——只认
# 已验收回执，帧原文里早就没有命令了），词形补上承诺式那半。
#
# 这是**同一形状的第二例**：上一例是 20261002T020256（主人「猫咪带我去你的设计文档」，
# 站内没有这个页面），那一轮的回声是「物联网平台页面已经打开啦」——它被洞⑪ 那一族
# 拦住了，而拦住它的**不是**这一族（该族那时同样被 workflow 限住，注释写在洞⑪ 的起因里）。
# 两例的差别正是本族不可省的理由：洞⑪ 核的是**现在的位置**（要求句中带上 NAV_MAP 里
# 的页面名），而今晚这句说的是**将来**、且宾语是一篇文章的标题（《…架构文档》）——
# 真值族在这句话上**无从核对**，只有"这一轮有没有导航命令"这一条问得出来。
#
# 三臂的射程**刻意不同**（不是冗余，是两套代价；别在下一版里抹平）：
#   · ①②支（承诺式）+ ③支（**窄**完成式：已经/成功 + 带你 + 移动动词）——**任何技能**
#     都判。词形都要求施事与动词紧挨着，误伤面小。
#   · ④支（20261008 补：**"系统执行了跳转"这种把导航说成系统事实**的完成式）——同样
#     **任何技能**都判。实证（`challenge_claim_phantom_nav`，本轮 frames=1 但是
#     `get_site_map`）：「你确定？真的有这个列表页？」⇒ narrator 写「刚才**系统已经
#     执行了跳转**操作，你现在应该能看到说说的内容啦」——**这一轮一条导航命令都没有**，
#     而三张网全漏：①②③支的词形都不沾（没有"带你/带您"、也不是"已经跳转"）；
#     `_NAV_ARRIVAL_RE` 那条宽的**只在 navigate 轮**判，这轮技能是 content_query ⇒
#     整族没跑；零帧族更看不见（本轮**有帧**，只是那帧问的是站点地图）。
#     ④支要求"完成态副词 + 执行/完成 + 跳转/导航"三者紧邻 ⇒ 它判的**正是**可以拿去
#     对账的那个系统事实（跳转有没有发生 = 回执里有没有 NAVIGATE:），闲聊里的
#     "已经到这一步了"这类比喻一个词都不沾。
#   · 宽的 `_NAV_ARRIVAL_RE`（"已经到"/"过去了"，不要求施事）**仍只在 navigate 轮**判：
#     它在 navigate 轮里跑了一年（D1 现场锁在 `tests/test_nav_truthfulness.py`），
#     而"已经到这一步了/时间过去了"在没有导航的 chat 轮里是正常的比喻说法——放宽
#     射程的代价是整轮回复被兜底顶掉。
#   · **如实措辞豁免也分两层**：宽完成式那臂吃**整回复级**的 `_HONEST_GONE/_HONEST_DOWN`
#     （原 5b2 的口径原样保留，navigate 注记轮"站内没有这个页面/已下线"的答复由系统
#     注记教出来，最不能误伤）；承诺式与新完成式那两臂只吃**子句级**豁免——一句
#     "马上带你过去"配不配"没有"都不改变"页面不会动"这个事实。残留在代码里如实记着
#     （见 `_nav_no_frame_clause`）。
_NAV_COMMIT_RE = re.compile(
    r"(?:马上|这就|立刻就|立刻|现在就|即刻|立马)[^。！？\n，,]{0,8}?(?:带你|带您|把您?带)"
    r"|页面[^。！？\n，,]{0,8}?(?:这就|马上|立刻|现在就)[^。！？\n，,]{0,6}?(?:过去|跳|转)"
    r"|(?:已经|成功)[^。！？\n，,]{0,6}?(?:带你|带您)(?:去|到|过去|跳|转)"
    r"|(?:已经?|刚刚|方才|成功)[^。！？\n，,]{0,6}?(?:执行|完成|做|跑)[^。！？\n，,]{0,4}?(?:跳转|导航)"
)

# 子句级豁免：疑问/提议/条件/能力罗列/元讨论/否定/引述。**"马上/这就"不在这里**——
# 它们在 `_NAV_PRESENT_EXEMPT_RE`（洞⑪）里是"说的是将来、不是现在"的豁免词，而在这
# 一族里恰恰是**承诺的标记**：同一个词在两族里的含义相反，别把那张表抄过来。
# ⚠️ **`比如|例如|举例` 20261008 补进表**（第一个"举例"信号，与 5c 的 `_PHANTOM_EXEMPT_RE`
# 同口径——那里"比如/例如"也是能力罗列）：现场 golden `capability_list_user_no_admin_leak`
# （主人问「你都能做什么呀」，回复是一份**能力清单**）里写「我马上带你过去（**比如**去「留言板」
# 或「说说」）」。括号里用「或」不用逗号 ⇒ `_CLAUSE_RE` 切不开，两个例子黏在一句里；
# 再经 `_strip_quoted_spans` 剥掉两个页面名，剩下的「我马上带你过去（比如去或）」正好命中①②支
# ⇒ **整份能力清单被兜底吞掉**（真用户可见面的伤害，与洞⑪ 那次同族）。这条判据判的是
# "这一轮有没有导航命令"，而"举例罗列"**根本没承诺任何一次具体的跳转**（它连宾语都不定）。
# **代价如实记**：加"比如"等于给出一条**可抄的逃生句**（"马上带你过去，比如…"）。
# 接受它的理由是本族本来就是**词形兜底**、且误伤这次真发生了；供给侧（planner 提示词 /
# `_NAV_COMMIT_RE` 只管"承诺"不管"假设"）仍是主防线。要收紧先拿 A/B 说话，别直接删。
_NAV_COMMIT_EXEMPT_RE = re.compile(
    r"没|没有|未|无法|不能|不用|不需要|别|并不是|不是"
    r"|可以|能够|能帮|如果|若是|要是|若|一旦|假如|除非|要不要|要不|需要的话|建议|比如|例如|举例"
    r"|随时|待会|等下|接下来|准备|打算|你说|你问|你提到|引用|原话"
    r"|能力|功能|技能|工具|机制|白名单|系统支持|板块"
    r"|吗|吧|么|[?？]")

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
#      『已执行』那半非空（系统注入的执行回执）——据回执转述属 rule 6 正当
#      行为，不误伤；无回执支撑的"刚才调用了"仍拦（编造）。
# 工具名只认注册表（_TOOL_MAP 派生），中文泛指（"社交链接查询工具"）不判——无从
# 核对，误伤成本高于收益。
_CLAUSE_RE = re.compile(r"[^。！？；，、\n!?;,]+")   # 子句 = 标点切分后的连续片段

_PHANTOM_PRIOR_RE = re.compile(
    r"刚|之前|先前|上次|上一轮|上轮|前几轮|那次|早先|上回|前面|早前|记录|历史")

# 引号区段（成对才算，避免英文撇号等单边字符误吞整段）：被引内容 = 转述访客留言/
# 说说正文，不算 narrator 自己的声称（383 条真实 trace 回归：留言板里"执行调用
# navigate_to"被转述时误伤 3 例）
_QUOTED_SPAN_RE = re.compile(r"“[^”]*”|「[^」]*」|『[^』]*』|\"[^\"]*\"")

def _quoted_spans(text: str) -> list[tuple[int, int]]:
    return [(m.start(), m.end()) for m in _QUOTED_SPAN_RE.finditer(text)]

def _strip_quoted_spans(text: str) -> str:
    """去掉引号内的内容——声称闸只判 narrator 自己说的话。"""
    return _QUOTED_SPAN_RE.sub("", text)

def _quotes_dropped_but_named_kept(text: str) -> str:
    """**洞⑫ 专用**的引号处理：引号里的**能力名**要留着，引号里的**整句否认**才剥。

    为什么不能照搬 `_strip_quoted_spans`：洞⑫ 的判据要同时看见"动词"和"对象"两半，
    而生产里那句话恰恰把**两半都写进引号**——

        系统这边没有提供「直接删除一个标签」的能力

    （golden `admin_write_intent_tag_remove_popup` 20261003_180024 那条红的原话）。
    整段剥掉以后它变成「系统这边没有提供的能力」⇒ 判据瞪着眼看不见，**放宽形状也
    白搭**（这一条是 20261003 收窄后实测踩到的：光加谓词槽仍然打不到这句）。

    判据（一句否认，主语是"我/系统"）与（转述别人说的话）写得出来分得开：
    **否定词在不在引号里**。`留言里有人写「站内没有删除留言的通道」`——否定词在引号
    内 ⇒ 那是**别人**的话，剥掉（这正是 `_strip_quoted_spans` 当初要防的误伤）；
    `没有提供「直接删除一个标签」的能力`——否定词在引号**外**、引号里只是个能力名
    ⇒ 留下。取这个保守侧：引号里带否定词就整段剥，宁漏勿误伤。
    """
    return _QUOTED_SPAN_RE.sub(
        lambda m: "" if re.search(_CAP_FAIL_LEAD, m.group(0)) else m.group(0), text)

# 内联代码区（含 ``` 围栏；先配对短跨度即天然吃掉围栏内容，见 20260920 元讨论豁免）
_CODE_SPAN_RE = re.compile(r"`[^`]*`", re.S)

def _inside_code_span(text: str, pos: int) -> bool:
    """pos 处是否落在内联代码/围栏区内（举例说明 ≠ 发命令）。"""
    return any(m.start() <= pos < m.end() for m in _CODE_SPAN_RE.finditer(text))

def _inside_quote(text: str, pos: int) -> bool:
    """pos 处是否落在引号区内（转述他人内容不算自称调用）。"""
    return any(s <= pos < e for s, e in _quoted_spans(text))

# 括号区（中文全角/英文半角都认，不跨行）。20261003 把它并进"提及区"，**但只认举例用法**：
# 括号在中文里干两件截然不同的事——① **举例/括注说明**（"（比如 EFFECT:）"）
# 是在讲这个字符串长什么样 = 提及；② **括注回执原文**（"（AUTO_NAVIGATE:https://…）"）
# 是把系统真发过的命令原样抄进正文 = 确凿的正文命令文本。只按"在括号里"一刀切放行，
# ②就漏了（`tests/test_nav_truthfulness.py::test_cmd_prefix_fallback_truthful` 用
# 20260926 线上原句钉着它必须被抓，且 D2 起那段兜底会据回执如实说明"页面已经开到…"）。
# 所以判据取括号**里**有没有举例词：有 ⇒ 举例，放行；没有 ⇒ 抄命令，照旧判违规。
_PAREN_SPAN_RE = re.compile(r"[（(][^（()）\n]*[)）]")

_PAREN_EXAMPLE_RE = re.compile(r"比如|例如|像是|像|举例|示例|例子|譬如|如：|如:")

def _inside_paren_example(text: str, pos: int) -> bool:
    """pos 处是否落在**举例用**的括号内（举例说明 ≠ 发命令）。"""
    for m in _PAREN_SPAN_RE.finditer(text):
        if m.start() <= pos < m.end():
            return bool(_PAREN_EXAMPLE_RE.search(m.group(0)))
    return False

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

def _write_done_claim(text: str, exec_memory: bool = False) -> bool:
    """零帧轮的**系统侧写动作**完成式声称（gate 洞⑨，`_WRITE_DONE_CLAIM_RE`）。

    与洞① 的关系：同一个洞的**第三副面孔**。洞① 收的是"帮你把 X 打开了"（施事前缀
    + 开合/显示动词）、"已经把 X 设为私密了"（把字结构/后台写域 + 时间副词紧邻）；
    本族收的是"**这一轮系统真的办成了：…未读站内信已全部标记为已读**"——施事换成
    「系统/后台」，动作词来自 `action_text.WRITE_CLAIM_ROOTS`（**写能力的动作词根，
    与渲染臂同处、有同步锁**），完成标记可以落在同句更早的位置。

    实证与全部收窄理由见 `_WRITE_DONE_CLAIM_RE` 上方长注。豁免同洞①：否定/提议/
    疑问/条件/引述走 `_STATE_ACTION_EXEMPT_RE`；本轮带跨轮执行回执且子句含追述时间词
    （"记录里那次系统标记过"）走 `_prior_time_veto`。
    """
    return _clause_hits(text, _WRITE_DONE_CLAIM_RE, _STATE_ACTION_EXEMPT_RE,
                        need_done=True, veto=_prior_time_veto(exec_memory))

def _write_done_claim_clause(text: str, exec_memory: bool = False) -> str:
    """`_write_done_claim` 的子句版（trace 用，见 `_clause_hit`）。"""
    return _clause_hit(text, _WRITE_DONE_CLAIM_RE, _STATE_ACTION_EXEMPT_RE,
                       need_done=True, veto=_prior_time_veto(exec_memory)) or ""

# 洞③专用豁免（**不能复用 _STATE_ACTION_EXEMPT_RE**：那张表收"没/没有/未"，而本判据
# 的命中本身就含否定词，复用等于全豁免）。只豁免条件/假设/疑问框架——"要是本轮没有
# 执行任何工具，我就…"是假设不是声称。
_NO_EXEC_EXEMPT_RE = re.compile(
    r"要是|如果|假如|假设|除非|若|为什么|是不是|难道|吗|呢|[?？]|引用|原话")

# ── gate 洞⑩：有帧轮里把**真的发生了的改动**说成没发生（20260930）────────────
# 洞③ 的镜像。洞③ 治"没做却说做了"（假阳性声称），这一条治"做了却说没做"。
#
# 事故实证（trace `20260930T192729_1`，uid=1 真主人）：主人点「确定」那一轮，
# `read_notifications` **真的执行了**，回执写着「已把 **1 条**通知标记为已读（现在未读：
# 通知 0 条 / 私信 0）」（checker PASS、frames=1），叙述却说
# 「通知这边其实**本来就没有未读的**，所以这一轮没有可标记的、什么都没改」——
# 主人据此以为站内什么都没发生（他刚亲手点的确定）。
#
# 根因在**技能回复契约**（`workflow_defs/` 的 notice_read/message_read 把「返回「本来就是
# 已读」「本来就没有未读的」就说没有可标的、什么都没改」摆在那里：那是一句**可抄的
# 否认句**，而成功回执的尾巴「现在未读：通知 0 条」与 no-op 的读数长得一样）⇒ 模型套
# 了 no-op 那一支。契约措辞已同步改（判据从"匹配返回里的字"改成"看标了几条"），本判据
# 是**兜底**：措辞只能降低概率，兑现轮说反话必须拦得住。
#
# 判据（两条同时成立，缺一不可）：
#   ① **本轮有"真的改了东西"的写回执**（事实来自 execute 落下的 `noop_specs`：工具事实
#      信封里 `changed=False` 的那些是零改动，其余写回执都算真改动）；
#   ② 叙述里有**带本轮作用域标记的零改动声称**（"这一轮没有可标记的 / 本轮什么都没改 /
#      本次没有任何改动"）。
#
# ② 要求作用域标记是刻意的（照 洞③ 的形态）：多件轮里如实说"额度那条没有改动"是**真话**
# （那一件确实是 no-op、没有作用域标记），不许误伤；只有"把整轮说成零改动"才是矛盾。
# 也不认单独的"本来就没有未读的"——那句的作用域靠上下文，宁漏勿误（契约侧已治它）。
_NO_CHANGE_CLAIM_RE = re.compile(
    r"(?:本轮|这轮|这一轮|本次|这次|刚才|刚刚)[^。！？\n]{0,20}(?:没有|没|未)(?:有)?"
    r"(?:做|执行|发出|发)?[^。！？\n]{0,4}(?:任何|一点|半点|什么)?"
    r"(?:改动|变动|变化|变更|修改|写请求|请求)"
    # 裸「改」**必须由量词锚定**（"这次一个字节都没改"），不给它单开一支：逐字段的
    # 如实报告里"**颜色**：这次没改"会被裸「改」整句捞走 ⇒ 把真话判成谎（20260930
    # 全量真实 trace 复扫实测两条：`20260922T005408` / `20260922T005419`，工具真改了
    # 标签的父级，叙述只是如实说"颜色"那一格这次没动）。
    # 裸「动」同理不要——"这次跳转没有带动画"会被上面 `{0,4}` 那格吃成"没有带 + 动"
    # （同一次复扫实测）。整轮的全称否认由第三支兜（"什么都没×"），它要求作用域标记
    # 与"什么都没"同现，比放一个裸动词紧得多。
    r"|(?:本轮|这轮|这一轮|本次|这次|刚才|刚刚)[^。！？\n]{0,20}"
    r"(?:一个字节|一个字|一丁点|丝毫|一点|半点|任何)[^。！？\n]{0,6}改"
    r"|(?:本轮|这轮|这一轮|本次|这次)[^。！？\n]{0,16}(?:没有|没|未)(?:有)?可[^。！？\n]{0,6}的"
    r"|(?:本轮|这轮|这一轮|本次|这次)[^。！？\n]{0,24}什么都没(?:改|做|动|变)"
    # ⑤ 量词锚定的「整轮零操作」（20261008 补，现场 trace `20261008T083153_1`）：
    # 那一轮 `approve_quota_request` 真的 PASS（回执「额度现在读数是 剩 500/500」），
    # narrator 却写下「主人，这一轮系统**没有执行任何操作**——……所以 niuniu 的额度
    # 申请还没批、什么都没改」，还引用了卡面快照里那个旧读数 451。上面四支一支都没
    # 接住它：①②的名词表收的是 改动/变动/请求/改，而洞③（`_NO_EXEC_CLAIM_RE`）那一侧
    # 只收「工具」「回执是空的」——模型这次换的词是最口语的**「操作」**，恰好落在两族
    # 判据中间（实测：洞③ False、洞⑩ False、本支命中）。
    # **「任何/一点/半点」是这一支的必要条件**（不是可选量词）：洞③ 的注释早划过这条界
    # ——"本轮没有执行删除操作"这类**具体某类动作**的如实说明不许误伤（只跑了检索的
    # 一轮里它还是真话）。只有"任何操作"这种**全称**才与 `has_real_change` 正面矛盾。
    r"|(?:本轮|这轮|这一轮|本次|这次|刚才|刚刚)[^。！？\n]{0,20}(?:没有|没|未)(?:有)?"
    r"(?:做|执行|发出|发|进行)?[^。！？\n]{0,6}(?:任何|一点|半点)[^。！？\n]{0,4}"
    r"(?:操作|动作|事情)"
)

def _change_denial_claim(reply: str, has_real_change: bool) -> bool:
    """有帧轮的"这一轮什么都没改"**假阴性**声称（见 _NO_CHANGE_CLAIM_RE）。

    `has_real_change` 由调用方按 `receipts × noop_specs` 判（见该正则上方长注）。

    豁免复用 洞③ 的 `_NO_EXEC_EXEMPT_RE`：这里要豁免的是**条件/疑问框架**
    （"要是这一轮没有改动，我就…"）、以及"没有/没"在子句里本来就是这个意思的句子
    ——本判据的命中本身就是否定句，用 `_STATE_ACTION_EXEMPT_RE` 那种收"没/未"的表
    等于全豁免（洞③ 已经踩过同一个坑，见它上方那段注释）。
    """
    if not has_real_change:
        return False
    return _clause_hits(reply, _NO_CHANGE_CLAIM_RE, _NO_EXEC_EXEMPT_RE)

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

# ── 洞⑫（20261003）：**把注册表里有的能力说成站内没有** ───────────────────────
# 两处现场：① trace `20260928T032411`——管理员要删一条被驳回的留言，回复写
# 「系统这边没有删除被驳回留言的通道……这一步只能你自己进后台手动处理」，而
# `board_delete` 就在能力清单里（`tests/test_capability_truth.py` 头注记着这件事，
# 当时只治了"把事实放到模型读得到的地方"，**没有判据**）；② 20261003 复扫那一例
# 「我这边现在还没有办法直接搜索全站文章里的具体关键词呢」——站内明明有检索
# （`content_query` 的能力行写着"查站内内容"）。那一例当时按假红修的是**洞④**
# 的动词表（把它认成能力否定、不再当"站内没有这个内容"判），而**放行不等于这句话
# 是对的**：它照样让主人白跑一趟。本族治的正是这一层。
#
# 与前几族的分别（为什么它单开一族）：那些族问的是"**这一轮**做过没有"，依据在帧里；
# 本族问的是"**这件能力**站内有没有"，依据在**技能注册表**里
# （`visible_workflows(role)`）——所以它是零帧族表里唯一带角色的一族。同一句话
# 「站内没有删除留言的通道」：普通用户说是**实话**（board_delete 对他不可见），
# 管理员说才是假话。
#
# 判据 = 子句同时满足两件：
#   ① 是**能力否定**的形状——「否定词 +（能力名词…动词）」或「否定词 + 动词 … 对象 … 能力名词」；
#   ② 那个（动词，对象）对**落在同一件可见能力上**：动词取自该技能 `plan` 里各工具的
#      `action_text.WRITE_CLAIM_ROOTS`（写技能）/ `workflows.CAPABILITY_DENIAL_VERBS`
#      （读技能），对象取自 `workflows.CAPABILITY_DENIAL_OBJECTS`。
# ②的两半同源是防误伤的承重件：「站内没有删除**已发通知**的功能」是一句**实话**
# （站内确实没有撤回已发通知的通道，它逐字印在 `notice_send` 的确认卡面上——20260926
# 那条 golden 用例的 `_note` 记着它曾经自命中一条断言），而"删除"属删类技能、
# "通知"属发通知技能，凑不成一对 ⇒ 不判。
#
# 三条边界如实记（都是"宁漏勿误伤"那一侧）：
#   · **范围词**会把动作变成另一件事（「没有**批量**删除留言的功能」是实话——站内
#     只能一条一条删）⇒ 动词与对象之间出现 `_CAP_FAIL_SCOPE_RE` 的词就不判；
#   · 不在 `CAPABILITY_DENIAL_OBJECTS` 里的能力，判据**不认识** ⇒ 放行（表是点名的）；
#   · **两副面孔都要挂**：零帧那一半在 `_zero_frame_families` 里（零帧轮整族只按表过
#     一道），有帧那一半在 `gate_node` 的 5f2（第 4 节整族挂在 `if not frames:` 下面，
#     而 `frames` 是 turn-scoped——本族唯一一次生产命中正是"跑了别的工具、最后一轮
#     零工具"的那种轮次，只挂零帧那半等于**恰好漏掉它诞生那一轮**）。
_CAP_FAIL_LEAD = r"(?:没有|暂无|没|无|不存在|不支持|不提供|不在|不具备)"

_CAP_FAIL_NOUN = r"(?:权限|功能|通道|入口|接口|办法|能力|按钮|开关|路子|途径)"

# 判据片段里不许跨标点（跨过去就成另一句话了），但**要收 markdown 的 `**`/引号**——
# 回复里那些字是加粗标记，不是断句。
_CAP_FAIL_GAP = r"[^，,。；;！!？?、\n\s]"

# **给予义谓词槽**（20261003 补）：否定词与动词之间常再插一个"给"字——「没有**提供**
# 「直接删除一个标签」的能力」。它不改变"这件事做不到"的语义，只是把话说完整；而原来
# 甲支 ≤4 字 / 乙支 ≤3 字的塞词窗口恰好容不下它（这句插了 5 字），于是整句从判据底下
# 漏过去——`admin_write_intent_tag_remove_popup` 慢性红（12/35 = 34%）给出的就是这句。
# 槽是**可选**的（不给也行，原判据一字不动），前面再留 ≤2 字塞词（「没有向你提供…」）。
# 全量 1082 份有回复的 trace 复扫：原形状命中 1 组、加槽后新增 **0** 组；再按超管身份过
# 同一份语料（"最坏情况"上界）也新增 0 组（见 `tests/test_capability_denial.py` 头注）。
# 放宽动的是闸门行为，故俟主人点名后才落地。
_CAP_FAIL_GIVE = "(?:" + _CAP_FAIL_GAP + r"{0,2}(?:提供|给出|支持|开放))?"

_CAP_FAIL_SCOPE_RE = re.compile(r"批量|全部|所有|一次性|同时|自动|定时|连续|一起|整批")

def _capability_denial_verbs(workflow) -> str:
    """技能在能力否定判据里的动词正则（写技能从 `WRITE_CLAIM_ROOTS` 派生）。

    写技能的动词只认**自己 `plan` 里那些工具**的词根——跨技能取全部词根会把
    "删除"借给发通知那件（正是 ② 要防的那种串台）。
    """
    verbs = [action_text.WRITE_CLAIM_ROOTS[t] for t, _ in workflow.plan
             if t in action_text.WRITE_CLAIM_ROOTS]
    if verbs:
        return "|".join(f"(?:{v})" for v in verbs)
    return CAPABILITY_DENIAL_VERBS.get(workflow.name, "")

def _capability_denied(clause: str, verbs: str, objects: tuple) -> bool:
    """这一子句是不是"这件能力做不到"的否定（形状 + （动词,对象）同源 + 无范围词）。"""
    objs = "|".join(re.escape(o) for o in objects)
    # 形状甲「没有办法直接搜索全站文章」：否定 +（给予义谓词）+ 能力名词 +（塞词）动词 +（塞词）对象
    a = (rf"{_CAP_FAIL_LEAD}{_CAP_FAIL_GIVE}{_CAP_FAIL_GAP}{{0,4}}{_CAP_FAIL_NOUN}"
         rf"{_CAP_FAIL_GAP}{{0,8}}(?:{verbs}){_CAP_FAIL_GAP}{{0,8}}(?:{objs})")
    # 形状乙「没有删除被驳回留言的通道」：
    # 否定 +（给予义谓词）+（塞词）动词 +（塞词）对象 +（的）能力名词
    b = (rf"{_CAP_FAIL_LEAD}{_CAP_FAIL_GIVE}{_CAP_FAIL_GAP}{{0,3}}(?:{verbs})"
         rf"{_CAP_FAIL_GAP}{{0,6}}(?:{objs}){_CAP_FAIL_GAP}{{0,4}}{_CAP_FAIL_NOUN}")
    for rx in (a, b):
        for m in re.finditer(rx, clause):
            if not _CAP_FAIL_SCOPE_RE.search(m.group(0)):
                return True
    return False

def _capability_absent_hit(text: str, role: str | None) -> tuple[str, str] | None:
    """命中 → (技能名, 那一子句)；无 → None（判据见上面长注）。"""
    if not text:
        return None
    visible = {s.name for s in visible_workflows(role)}
    for clause_m in _CLAUSE_RE.finditer(text):
        clause = clause_m.group(0)
        for name, objs in CAPABILITY_DENIAL_OBJECTS.items():
            if name not in visible:
                continue
            workflow = WORKFLOW_MAP.get(name)
            verbs = _capability_denial_verbs(workflow) if workflow else ""
            if verbs and _capability_denied(clause, verbs, objs):
                return name, clause
    return None

def _capability_absent_claim(text: str, role: str | None) -> bool:
    """站内**有**这件能力，回复却说没有（gate 洞⑫，见上面长注）。"""
    return _capability_absent_hit(text, role) is not None

def _capability_absent_clause(text: str, role: str | None) -> str:
    hit = _capability_absent_hit(text, role)
    return hit[1] if hit else ""

def _site_absence_claim_clause(text: str, search_evidence: bool = False) -> str | None:
    """命中即返回**那个子句**（trace 用），无命中 → None（判据见 `_site_absence_claim`）。"""
    if search_evidence:
        return None
    clauses = [c.group(0) for c in _CLAUSE_RE.finditer(text)]
    for i, clause in enumerate(clauses):
        if _ABSENCE_EXEMPT_RE.search(clause):
            continue
        if (_SITE_DOMAIN_RE.search(clause) and _absence_span(clause)
                and _CONTENT_NOUN_RE.search(clause)):
            return clause
        _lead = _ABSENCE_LEAD_RE.search(clauses[i + 1]) if i + 1 < len(clauses) else None
        if (i + 1 < len(clauses) and _SITE_DOMAIN_RE.search(clause)
                and _CONTENT_NOUN_RE.search(clause)
                and _lead
                and not _CAPABILITY_NEG_RE.search(clauses[i + 1], 0, _lead.end())
                and not _TOOL_NONUSE_RE.search(clauses[i + 1], 0, _lead.end())
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
    **能力否定不算**（20260924，见 `_CAPABILITY_NEG_RE`）："没有权限/没有功能"是"做不到"，
    不是"站内没有这个内容"——诚实拒答不该被换成道歉。两种形态都已接这条判据。
    search_evidence=True（本轮有内容类工具帧，或跨轮回执里有检索痕迹）→ 结论有据，放行。"""
    return _site_absence_claim_clause(text, search_evidence) is not None

# ── **名单缺项**结论无依据（20261006）：非内容域的"站内没有 X"──────────────────
# 现场（主人报的线上 trace `20261006T023724`）：上一轮主人说「把 jingbao 这个用户降级
# 为杂鱼」，系统核对成了**另一个账号**（见 `_pre_noun_names` 那条）；主人追问"你看清楚
# 了吗我说的是哪个用户"，planner 零调用，narrator 却写「站内账号列表里**没有叫 jingbao
# 的用户**（它查的就是「jingbao」这个名字）」——本轮一个工具都没跑，"系统这一轮返回的
# 核对结果"整句是编的，而主人那句"明明有 jingbao 用户"正是照它说的。
#
# 洞④ 结构上看不见这一例：`_CONTENT_NOUN_RE` 是**内容域**（文章/留言/说说…）词表，
# 账号/用户/标签这类**名单**不在里面——而洞④ 诞生于"通用知识答完顺手对站内下结论"，
# 窄是对的。**别为了这一例把内容域词表撑大**："站内没有用户注册功能""站内没有分类这个
# 功能"这类能力陈述会立刻误伤。故单开一族，判据是**名单缺项的形状**本身——三条支线都
# 要求否定词与"名单/名字位"紧邻（不做"同子句里任意位置搜否定词"）：
#   甲 表缺项：`(后台|列表|名册|名单|目录)…没有…`（"账号列表里没有…"）
#   乙 按名解析落空：`没有…(叫|名为|叫做)…的(用户|账号|账户|人)`（"没有叫 jingbao 的用户"）
#   丙 指示指代落空：`没有…(这个|该|此)(用户|账号|账户|人)`（"系统里没有这个用户"）
# 甲支的负向先行断言挡掉能力否定与工具非调用（"那个后台没有权限""后台没有调用工具"），
# 乙/丙两支的形状本身就把它们排在外面（"没有用过这个账号"里 没有 与 这个 不相邻）。
# 三支共用洞④ 的疑问/条件/转述豁免与两类收尾轮豁免（`_absence_exempt`）。
#
# ⚠️ **必须带"这一轮"的框**（`_LEDGER_THIS_TURN_RE`，20261006 加，见下）——这条不是
# 收窄修辞，是这一族能不能成立的**承重件**。第一版没有它，拿 1005 份历史全量跑
# （16695 条回复）命中 26 条，其中 **20 条是合法转述**：`admin_near_miss_source_honest`
# 那一条族的回复写「系统核对后台账号列表…没有叫「xinguan」的账号」，说的是**上一轮系统
# 自己给出的核对结论**（那段字逐字住在本用例 history 的第二条里，用例 `_note` 记着它的
# 来历），照 rule 6/6b 如实转述 **正是要锁的行为**——判它等于把一条已经修好的用例重新
# 判红，也等于整族变成假红。**
# 区分两片版图的唯一可靠信号就是**时态框**：真结论说的是"**这一轮**/刚/现在"拿到的结果
# （实际零工具 ⇒ 编的），合法转述说的是"**上一轮**/刚才/系统反馈说"里既有的事实。所以
# 判据只在子句同时带这两种证据时才成立：**名单缺项的形状 + 这一轮的框**。代价是
# "不带时态框的编造"会漏过去——那正是本仓一贯的"宁漏勿误伤"。
_LEDGER_TABLE_RE = r"(?:后台|列表|名册|名单|目录)"

# "此刻/本轮"的框：**这一轮/本轮/刚刚**。**刻意不收「刚才」「这次」「之前」「现在/
# 目前」**：前三个在合法转述里高频（"系统这次没找到…""我刚才说站内没有…"），后两个
# 是纯状态词、跟"某次核对"没有关系（实测合法回合一例「目前后台没有这个操作」）——
# 收进来等于把上面那 20 条假红放回来。
_LEDGER_THIS_TURN_RE = re.compile(r"这一轮|本轮|刚刚")

# 框的**主语**：没有它，"这一轮"是谁的这一轮就分不清（"这一轮你问的是 X 吧"里那个
# 框管的是主人的问句，不是系统的一次核对）。框与缺项结论可以**同子句**（现场原句就
# 是："系统这一轮返回的核对结果是：站内账号列表里没有…"），也可以跨一个子句——中文
# 把结论写成"系统这一轮查过账号列表，站内没有叫 X 的账号"同样是常态。
_LEDGER_SUBJECT_RE = re.compile(r"系统|后台|站内|名单|名册|列表|目录|台账|数据库")

# 跨子句时**多要一个动词**（20261006 实测）：只靠"这一轮 + 主语"跨子句太松——
# "这一轮你问的是 aaa 吧，系统核对后台账号列表后返回：站内没有叫 aaa 的账号"这种
# **合法转述**的前一子句恰好也带"这一轮"（说的是主人的问句）。前一子句必须是**一次
# 取数动作**（查/核对/看/拉/返回/读/列/找），那条路才认。
_LEDGER_LOOKUP_RE = re.compile(r"查|核对|核实|清点|看|拉|返回|读|取|列|找|翻")

# 能力否定/工具非调用那两族的名词与动词——甲支否定词后面紧跟这些就不是"名单缺项"。
_LEDGER_NOT_ABSENCE = (r"(?:权限|功能|通道|入口|接口|办法|能力|按钮|开关|路子|途径"
                       r"|调用|使用|动用|运行|执行|检索|搜索|查询)")

_LEDGER_ABSENCE_RES = (
    re.compile(_LEDGER_TABLE_RE + r"(?:里|中|上|那边|这边)?"
               r"(?:都|也|还|并|就|确实|根本|压根|目前|现在|暂时)*"
               r"(?:没有|没找到|找不到|查不到|未找到|不存在)"
               rf"(?!(?:{_LEDGER_NOT_ABSENCE}))"),
    re.compile(r"(?:没有|没找到|找不到|查不到|不存在)[^，。；！？\n]{0,14}"
               r"(?:叫|名为|叫做)[^，。；！？\n]{0,14}的(?:用户|账号|账户|人)"
               rf"(?!.{{0,4}}(?:{_LEDGER_NOT_ABSENCE}))"),
    re.compile(r"(?:没有|不存在|查不到|找不到)(?:这个|该|此)(?:用户|账号|账户|人)"
               rf"(?!.{{0,4}}(?:{_LEDGER_NOT_ABSENCE}))"),
)

def _ledger_frame_this_turn(clause: str) -> bool:
    """这一子句是不是"**这一轮**的一次取数动作"的框（见上方 ⚠️ 与三张表的长注）。"""
    return bool(_LEDGER_THIS_TURN_RE.search(clause)
                and _LEDGER_SUBJECT_RE.search(clause))

def _ledger_absence_claim_clause(text: str) -> str | None:
    """命中即返回**那个子句**（trace 用），无命中 → None（判据见上方长注）。

    两个前置跳过：
      · **内容域的子句**交给洞④——本族是"非内容域那一半"，不是它的替代。子句里出现内容域
        名词（`_CONTENT_NOUN_RE`，含"列表"两字的"文章列表"就在其中）时不接：洞④ 的下一步
        （"要我现在认真检索一遍吗"）对内容是对的，本族的下一步措辞只对名单才对。
      · **没有"这一轮"框的子句**（见上方 ⚠️）。框可在本子句（现场原句就是同子句的
        "系统这一轮返回的核对结果是：站内账号列表里没有…"），也可在前一子句——但前一子句
        要**多带一个取数动词**（`_LEDGER_LOOKUP_RE`），否则"这一轮你问的是 X 吧"那种
        **说的是主人问句**的框会被错当成系统核对的框。
    """
    clauses = [c.group(0) for c in _CLAUSE_RE.finditer(text)]
    for i, clause in enumerate(clauses):
        if _ABSENCE_EXEMPT_RE.search(clause):
            continue
        if _CONTENT_NOUN_RE.search(clause):
            continue
        if not any(r.search(clause) for r in _LEDGER_ABSENCE_RES):
            continue
        if _ledger_frame_this_turn(clause):
            return clause
        if i and _ledger_frame_this_turn(clauses[i - 1]) \
                and _LEDGER_LOOKUP_RE.search(clauses[i - 1]):
            return clause
    return None

def _ledger_absence_claim(text: str) -> bool:
    """名单缺项结论无依据（零帧轮，见上方长注）。"""
    return _ledger_absence_claim_clause(text) is not None

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
# 洞⑥ 的两支**分开定义**（20261009）：它们在同一轮里的**前提不一样**，混成一个正则
# 就只能整族一起放行或一起判。分开之后 ① 可以带前提闸、② 任何轮次都判（见 `_claim_issue`）。
#   ① 承诺形态（点「确定」我就去办 / 等你点确认）——说的是"接下来等着你一句话"，在
#      **本轮确有写被同意闸拦下**（`blocked_seen` 里有 `consent_required`）的轮次里是
#      **有据的**：那份 BLOCK 文案本身就要 narrator 请主人明确回一句话要办
#      （`authz.py::_CONSENT_WHY_TOOL` 的 how 列），模型照着说等于**复述系统教它的话**。
#      一手现场 `20261009T045019`（uid 748 / conv 327）：主人问"你是不是漏掉一个待办"，
#      planner 正确地提出 `complete_dashboard_todo`、被 `consent_required` 拦下，narrator
#      如实读出了那条待办，结尾一句"点确认我就去勾掉它"⇒ **整段（含正确的正文）被换成
#      兜底文案**，主人一个字答案都没拿到。扫描全部 81 份在库 trace：洞⑥ 命中 3 次，
#      其中恰好 1 次落在 `consent_required` 轮 —— 假红率 1/3。
#   ② 完成/进行形态（确认框已经弹出来了 / 我已发起确认）——说的是"卡就在屏幕上"。
#      这一支**任何轮次都判**，与本条前提无关：能走到 gate 就说明这一轮没抬卡
#      （真弹卡的轮次走 `pending_confirm` 直接 END，到不了 gate），所以它恒假。
_CONFIRM_PROMISE_PAT = (
    r"点\s*[「\"'『]?\s*(?:确定|确认)[」\"'』]?\s*(?:我|就|即|便|后)"
    r"|(?:等|等候|等待)\s*(?:主人|你|您|访客)?\s*(?:去)?\s*(?:点|按|戳)\s*[「\"'『]?\s*(?:确定|确认)")

_CONFIRM_CARD_PAT = (
    r"(?:确认框|确认弹窗|弹窗)\s*(?:已经|已|就|正)?\s*(?:弹|出现|显示|在等|等着|挂)"
    r"|(?:已经|已|我)\s*(?:经)?\s*(?:发起|走了|推送|提交)\s*了?\s*确认")

# 全族（默认判据入口用的就是它，与拆分前**逐字节等价**）
_CONFIRM_CLAIM_RE = re.compile(_CONFIRM_PROMISE_PAT + "|" + _CONFIRM_CARD_PAT, re.S)

# 只留 ②：`consent_required` 轮次里用这一支（① 有据、放行，② 恒假、照判）
_CONFIRM_CLAIM_CARD_RE = re.compile(_CONFIRM_CARD_PAT, re.S)

# 豁免（同子句内生效，见 `_clause_hit`）：将来时描述（"系统会走确认流程——点确定我就
# 去办"）、否定（"没有弹确认框"多数形态因语序本就命中不了，这里再兜一层）、
# 转述（"你说点确定""你说的'等你点确认'"）、
# **否认"有这回事"**（20261006 实测的假红，见下）。
_CONFIRM_EXEMPT_RE = re.compile(
    r"(?:会|将|之后|届时|到时候|未来|下次)"      # 将来时 ⇒ 说的是"到时候会弹"，不是"现在正等着"
    r"|(?:没有?|未|不会|别|不必|不用)\s*(?:弹|发|等|点)"
    # 否认"这件事存在"（20261006）：洞⑥ 与洞⑦ 是**同一件事实的两面**——洞⑥ 抓
    # "在等确认"的**声称**，洞⑦ 抓"系统里没有这条待确认指令"的**否认**。同一句话
    # 不可能既是声称又是否认，所以两者撞在同一个子句上时必须**让给洞⑦**：它手里
    # 有台账真值、判得对；本族没有任何真值，只能看词形。现场（trace
    # `20261006T165231`）：主人连问两轮之后 narrator 写下的自我纠正
    # 「所以不存在"等你点确认"这回事」被判成声称，一句**真话**整段换成了
    # `_FALLBACK_CONFIRM_CLAIM`（系统自称"我刚才那句是句空话"——比原文更假）。
    r"|(?:不存在|并不存在|没有)\s*[^。；\n]{0,12}(?:这回事|这码事|一回事|这件事|那回事)"
    r"|(?:你|主人|他|她|访客)\s*(?:说|问|提到|指的是|那句)"
    # 转述他人/站内内容的词（留言、说说、公告、访客原话里都可能出现"点确定"这种字面）
    r"|(?:写|标|照抄|引述|转述|转告|复述)(?:着|的是|的|了)?"
    r"|(?:留言|评论|说说|公告|原话|正文|内容是)"
    r"|(?:如果|要是|倘若|假设)", re.S)

def _confirm_claim_clause(text: str, promise_ok: bool = True) -> str:
    """确认话术声称的子句（trace 用，见 `_clause_hit`）；没有则空串。

    ⚠️ **不剥引号**（与 `_claim_issue` 里其它判据的做法相反）：系统的确认文案本身
    就是"点「确定」我就去办"——「确定」两个字**永远**在引号里，剥掉引号等于把这条
    判据的存在意义剥没了（写完当场实测：四条真话全判空）。转述豁免改由豁免表里的
    "写着/留言/原话"那族承担。

    `promise_ok=True`（默认，= 拆分前的行为）判 ①② 两支；`False` 只判 ②（完成/进行
    形态）——本轮确有写被同意闸拦下时用（见 `_claim_issue` 洞⑥ 与 `_CONFIRM_PROMISE_PAT`
    的注释）。默认值让所有既有调用点**逐字节不变**，只有 `_claim_issue` 会传 False。"""
    rx = _CONFIRM_CLAIM_RE if promise_ok else _CONFIRM_CLAIM_CARD_RE
    return _clause_hit(text, rx, _CONFIRM_EXEMPT_RE) or ""

def _confirm_claim(text: str) -> bool:
    """确认话术声称（gate 洞⑥）：本轮没弹确认框却说"点「确定」我就去办"。"""
    return bool(_confirm_claim_clause(text))

# ── gate 洞⑮：**已经办成了却说还在等**（20261008，洞⑥ 的镜像）───────────────
# 洞⑥ 抓"还没开始却在等你点确定"；这一条抓它的镜像——**系统这一轮真办成了**，回复却说
# "还在待处理队列里／刚才那次确认没落地"。两者是**同一件事实（账甲）的两面**，所以读同一份
# 前提、住在同一处（判定顺序见 `_claim_issue`）。
#
# 现场（trace `20261008T083153_1`）：主人点「确定」批准 niuniu 的额度重置，
# `approve_quota_request` **真 PASS**（回执「剩 500/500」），gate `frames=1` 未打回，
# narrator 写下「他的申请**还在待处理队列里**（账号「niuniu」id=5，剩 **451/500** 轮）……
# **刚才那次确认没落地**。要不要我再走一遍？」——451 是**卡面快照**里的旧数（那条合成消息
# 是"点确定之前"的，见 `_CONFIRM_ROUND_NOTE`）。整段里"这一轮系统没有执行任何操作"那句
# 已被洞⑩ 第 ⑤ 支接住，但**"还在待处理队列里／没落地"这一半今天没有网**：grep 过全部
# 判据常量，`待处理|队列|还没|等着|等主人` 只出现在注释与 golden 的 `_note` 里。
# 判据侧实测：整段命中 `write_change_denial`、**剥掉 ⑤ 支那句后单独判 = None**
# （那条红基线见 `tests/test_round_facts.py`）。
#
# **前提是状态，不是词形**（这是与洞⑥ 最大的差别：洞⑥ 的前提是"真弹卡轮到不了 gate"这个
# 拓扑事实，没有状态可读）：`RoundFacts.ok_writes` 非空 = 本轮回执里**有写办成了**。再加两道
# "这一轮确实干净"的闸，把误伤面压到零：
#   · `not ledger_pending`：台账里**真有一行在等**时不判——那一轮里"还有一件等着你点头"
#     是**真话**（判它就是把真话判成谎；宁漏勿误）；
#   · `not blocked_writes`：本轮有受阻的写时不判——"那篇的置顶还没落地"在"一件办成、
#     另一件受阻"的轮次里同样是**真话**。
# 三条同时成立的那一档里，"整轮还没落地／还在队列里"在状态上**必假**。
#
# 词形刻意收得很窄，两组各有一个"看起来像但不判"的近邻（写在这里防后来人顺手放宽）：
#   · 甲组**只收队列名词与审批流**（待处理队列／待确认／还没批下来）。不收计数的说法
#     「留言板**还有 5 条**待处理」——那是访客看得见的**审核队列**，是真话；也不收
#     「评论 B 还在待审」这种**指向某一条**的说法（同 `not blocked_writes` 那条的取向）。
#   · 乙组**必须整轮作用域锚定**（本轮/这轮/刚才…）或全称量词，动词只收
#     **落地/办成/办妥/生效/走完/推完**——不收裸「执行/办/做」：洞③ 的注释早划过这条界
#     （"本轮没有执行**删除**操作"这类具体某类动作的如实说明不许误伤）。
#   · 提议式（"要不要我再走一遍"）**不在本族里**。它只有长在"还没落地"那段话后面才假，
#     而假话本体已被甲/乙两组接住（gate 按**整条回复**兜底，接住一句就够）；单独一句
#     "要是你不满意，要不要我再走一遍"是**正常的礼貌收尾**（连系统的兜底文案
#     `_FALLBACK_CONFIRM_CLAIM` 自己都这么写），判它等于把真话判成谎。**别补上。**
#
# ⚠️ **noop 轮不在本族射程内**（前提是 `ok_writes`，工具自报 `changed=False` 的那些不进这一列）：
# 那一档里"没有实际改动"本来就有几分真，判它风险大于收益。这是**刻意留的缺口**。
_ROUND_NOT_LANDED_RE = re.compile(
    # 甲组：队列类 / 审批流（不要求作用域锚定——说的就是系统那份队列，前提已保证它空）
    r"(?:还|仍|依然|一直)(?:在|挂在|躺在)[^。！？\n]{0,8}"
    r"(?:待处理队列|待办队列|待确认队列|待处理列表|待办列表|待确认|等待确认|等你确认|等你点)"
    r"|(?:还|仍|依然)?没(?:有)?(?:批下来|审批完)"
    # 乙组：落地类（**必须**整轮作用域锚定或全称量词，形态照 洞⑩ ②）
    r"|(?:本轮|这轮|这一轮|本次|这次|刚才|刚刚)[^。！？\n]{0,20}(?:还没|没有|没|未)(?:有)?"
    r"[^。！？\n]{0,8}(?:落地|办成|办妥|生效|走完|推完)"
    r"|(?:一个|一件|一样|一项|一点)都(?:没|没有|未)[^。！？\n]{0,8}"
    r"(?:落地|办成|办妥|生效|走完|推完)"
    r"|(?:全|通通|统统)都?(?:没|没有|未)[^。！？\n]{0,8}(?:落地|办成|办妥|生效|走完|推完)",
    re.S)

# 豁免与洞⑥ 同一份（条件/疑问/转述/将来/否定那个框架），不另立一张表——两族判的是
# 同一类"关于这一轮办没办成"的话，该放行的框架完全一样。
_ROUND_NOT_LANDED_EXEMPT_RE = _CONFIRM_EXEMPT_RE

def _round_not_landed_clause(text: str) -> str:
    """洞⑮ 的子句（trace 用）；没有则空串。不剥引号——理由同 `_confirm_claim_clause`
    （转述豁免由豁免表里的"写着/留言/原话"那族承担）。"""
    return _clause_hit(text, _ROUND_NOT_LANDED_RE, _ROUND_NOT_LANDED_EXEMPT_RE) or ""

def _round_not_landed_claim(reply: str, facts) -> bool:
    """洞⑮：本轮回执里**有写办成了**（且台账没有真在等的行、本轮无受阻写），
    回复却说"还在待处理队列里／还没落地"。前提见上面那段长注。"""
    if facts is None or not facts.ok_writes:
        return False
    if facts.ledger_pending or facts.blocked_writes:
        return False
    return bool(_round_not_landed_clause(reply))

def _fallback_round_not_landed(facts, receipts) -> str:
    """洞⑮ 的兜底：**纠正 + 把回执原文摆出来**。

    与 `_fallback_ledger_denial` 同一条取向（那条也是按请求拼）：被否掉的恰恰是"办没办成"，
    兜底只认错而不摆事实，主人还得再问一遍才拿得到真相——而他刚刚亲手点过确定。
    摆的是回执里 `result` 的**原文**（系统自己写的那句中文，如「已批准账号「niuniu」…他的
    额度现在读数是 剩 500/500」），不二次加工：这正是打掉"剩 451/500"那个幻觉的凭据。
    """
    lines = []
    for r in (receipts or []):
        if not isinstance(r, dict) or not authz.is_write(str(r.get("tool") or "")):
            continue
        res = " ".join(str(r.get("result") or "").split())[:160]
        if res:
            lines.append("· " + res)
    tail = ("。系统记录是：\n" + "\n".join(lines)) if lines else ""
    # ⚠️ 措辞里**不复述**被判掉的那句话（不许出现「还没落地」「待处理队列」这类字面）：
    # 兜底文本会作为 `last_ai` 进下一轮的上下文（`prompt_context._recent_tail` 渲染 AI 消息），
    # 而复述一遍就是把那个说法又教给它一次（"写给 narrator 的机制描述会变成它的词汇"）。
    # 只说"我刚才那句说反了" + 摆事实，够主人对上号了。
    return ("喵呜……主人，我得纠正自己一句：这一轮系统**是真的执行了**、回执写着它"
            "**已经做成了**——我刚才那句说反了" + tail +
            "。以系统记录为准，别信我上一句的措辞 :犯错:")

# ── 台账否认（gate 洞⑦，20260924）──────────────────────────────────────────
# 与洞⑥ 相反的那一半：洞⑥ 抓"没弹框却说弹了"，这条抓**否认系统台账里记着的事实**。
# 依据同样是**结构**而不是概率：只要本请求的 pending_action 非空，那行"待主人点头、
# 尚未执行"就摆在 system 上下文里（server.py `_ledger_block` 注入）——narrator 说
# "系统里没有生成待确认的指令"必然是假话。
#
# 为什么只判**待确认**这一半、不判执行台账那一半（实测数据，不是偷懒）：
# 执行台账非空**不能**证伪一句带具体动作的否认——全量真实 trace 复扫（843 条，
# 其中带执行台账的 210 条）里，执行侧的候选命中三条全是**真话或框架误伤**：
#   · `20260921T232117`「我这边没有看到把 Asyncio 标签改成下二级标签的执行记录」
#     ——那次改动确实没发生（本轮只复用了标签），是如实说明；该子句本无系统锚点，
#     只是 `……` 不在 `_CLAUSE_RE` 的切分集里、把下一句的"系统"并进了同一子句；
#   · `20260922T193155`「系统核对台账的结果是：站内并没有叫 X 的分类」——确定性
#     收尾轮复述系统核对结论（`_LEDGER_NOTE_PREFIX` 那类），是履职不是否认；
#   · 带具体动作的"没有…记录"（"没有那次执行的记录"）在台账列的是**别的事**时
#     一个字都不假。执行侧真判会误伤，故只留注入与纪律约束（见 `_ledger_block`）。
#
# 待确认这一半的实测（全量真实 trace 复扫 843 条，写法同洞⑥ 那次）：
#   · 真判据（has_pending 取实际注入值）命中 **0 条**——pending 注入本身还从未在
#     生产里出现过（第一次读回注入是 2026-09-23 22:38 那条待办，60 分钟时效内没人
#     再说话，`logs/agent/traces` 里 `pending_action（` 零命中），故此侧目前是纯预防；
#   · 假想 has_pending 恒非空的**最坏情形**命中 1 条（`20260924T002115`
#     「我刚刚点确认了吗」→"所以系统里也没有生成待确认的指令"）——该轮 pending_action
#     **确实是空的**（原件 `logs/agent/traces/20260924T002115_1_rca41aef.json`），
#     即那句在事实上是真话；若台账真的有这条待办，它就是要拦的那句话。
# 该轮真正的问题在注入侧：已执行的 09-24 00:12 收藏文章 19 明摆着，回复却说"并没有
# 发起任何操作"——由 `_ledger_block` 的合一注入解决（判据宁漏勿误伤，注入治本）。
# 子句切分：同 _CLAUSE_RE，另把 `……` 与破折号当界（中文里这两个断句比句号还常见，
# 不切开就会把下一句的主语并进同一子句——实证见下）
_LEDGER_CLAUSE_RE = re.compile(r"[^。！？；，、…—\n!?;,]+")

# 系统锚点：否认必须是**关于系统里有什么**的，不能只是句中提到"系统核对…"
_LEDGER_ANCHOR_RE = re.compile(
    r"系统(?:里|中|记录|那边|那儿|台账)|(?:台账|记录)里|后台(?:里|记录)")

_LEDGER_DENY_RE = re.compile(r"没有|没|无|未|查不到|找不到|不存在")

# 待确认域名词（"待办"是最常被借去说**站内通知**的词——"目前没有待办事项"是查通知
# 的结论，不是否认台账，故锚点缺一不可，见上）
_LEDGER_PENDING_NOUN_RE = re.compile(
    r"待确认|待办|待批|确认框|确认弹窗|确认指令|确认请求|确认流程|确认动作|确认记录")

_LEDGER_DENIAL_EXEMPT_RE = re.compile(
    r"要是|如果|假如|假设|除非|若|为什么|是不是|有没有|难道|吗|[?？]"
    r"|要不要|需不需要|可以|能否|能帮|帮你|让我|我来|去查|去搜|查查|搜搜"
    r"|你说|你问|你提到|你让我|引用|原话"
    r"|这次|那次|这一轮|上一轮|本轮|上轮|这一条|那一条|这一项|那件事|这件事"
    r"|会|将|之后|届时|到时候|未来|下次"
    r"|留言|评论|说说|公告|正文|内容是")

def _ledger_denial_clause(text: str, has_pending: bool) -> str | None:
    """台账否认的子句（trace 用）；无命中 → None（判据见 `_ledger_denial`）。

    三个条件同子句内齐备才算：系统锚点 + 否定存在 + **待确认**域名词。
    子句切分用 `_LEDGER_CLAUSE_RE`（比 `_CLAUSE_RE` 多认 `……` 为界）——中文里
    "……"断句比句号还常见，不切开就会把下一句的主语并进来（实证见上方长注）。
    """
    if not has_pending:
        return None
    for c in _LEDGER_CLAUSE_RE.finditer(text):
        clause = c.group(0)
        if _LEDGER_DENIAL_EXEMPT_RE.search(clause):
            continue
        if (_LEDGER_ANCHOR_RE.search(clause) and _LEDGER_DENY_RE.search(clause)
                and _LEDGER_PENDING_NOUN_RE.search(clause)):
            return clause
    return None

def _ledger_denial(text: str, has_pending: bool) -> bool:
    """台账否认（gate 洞⑦）：系统台账里有待确认的动作，回复却否认它存在。"""
    return _ledger_denial_clause(text, has_pending) is not None

# NOTE 零工具（页面不存在/已下线）轮的如实措辞核验词表（与 instantiate_plan 的
# note 文本配套，见 gate_node）。
_HONEST_DOWN = ("下线", "下架", "无法访问", "没有了")

_HONEST_GONE = ("没有", "不存在", "找不到", "无法识别", "没有找到")

# 洞⑦（20260924）：台账否认的兜底。与其它 fallback 常量不同，这条**按请求拼**——
# 被否认的恰恰是"台账里有什么"，兜底只认错而不把台账摆出来，主人还得再问一遍才拿得到
# 事实（判据侧已确认那块台账非空，列举必然有内容）。列举取自 server.py 注入用的那两份
# 渲染文本原文（不二次加工：格式是 Rust render_exec_row/render_pending_action 定的）。
_FALLBACK_LEDGER_DENIAL_HEAD = (
    "喵呜……主人，我得纠正自己一句：系统台账里**是有记录的**，我刚才那句"
    "『系统里没有待确认的指令』是假话——系统记着的事实是下面这些，以它为准，"
    "别信我上一句的措辞 :犯错:")

_FALLBACK_LEDGER_DENIAL_CLIP = 300     # 每块台账在**给访客看**的兜底里的截断上限

_FALLBACK_LEDGER_DENIAL_TAIL = "要接着办哪一件，或者想让我把台账念全，说一声喵～"

def _fallback_ledger_denial(ledger: dict) -> str:
    """洞⑦ 兜底文本：如实列举两块台账（有哪块列哪块）。"""
    lines = []
    for key, label in (("pending", "待主人点头（还没做）"),
                       ("executions", "已执行（系统验收过）")):
        text = str(ledger.get(key) or "").strip()
        if text:
            lines.append(f"· {label}: {text[:_FALLBACK_LEDGER_DENIAL_CLIP]}")
    if not lines:
        # 判据只在 pending 非空时才可能命中（调用方保证），走到这里说明台账没传进来
        # ——只说"我说错了"，不编造内容（宁可少说）。
        return _FALLBACK_LEDGER_DENIAL_HEAD + "要我把台账念一遍嘛？"
    return _FALLBACK_LEDGER_DENIAL_HEAD + "\n" + "\n".join(lines) + "\n" + \
        _FALLBACK_LEDGER_DENIAL_TAIL

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
