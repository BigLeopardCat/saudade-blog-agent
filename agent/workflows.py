# -*- coding: utf-8 -*-
"""技能注册表：固定行为（固定流程任务）的静态定义。

产品级"plan 写进 workflow"的落地：执行步骤是模板化数据，不是模型编的自由文本。
planner 只从本注册表选技能 + 填参数（受限规划，不再自由写 STEPS），
instantiate_plan 把模板实例化为计划（两态一次写入：对象是唯一事实源、文本是渲染，
含 TOOLS 行执行清单），execute 确定性逐条执行。

导航映射表是本模块的单一事实来源（页面别名→路径，替代散落在 prompt 里的白名单）：
planner 选 navigate 技能时能看到映射表，"去物联网平台"→ /device-console/ 的
业务知识从此属于系统数据而非模型猜测（修复 planner 跑题的根因）。

架构位置：
  planner（选技能 + 填参数）→ instantiate_plan（技能模板实例化 plan 文本，
  TOOLS 行 = 执行清单）→ execute（确定性逐条执行）→ model（零工具叙述）→ gate（确定性检查）
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

# 依赖导入（20261012 统一上移）：这一组此前夹在模块级数据（`_NEAR_MISS_CONTRACT`、
# `NAV_MAP`）之后，是 ruff E402 全部 27 条里**唯一不在脚本里**的一组——而
# `docs/lint-baseline.md` 当时把 E402 一律说成"脚本形态"，口径与事实不符。
# 位置不改变求值时刻（模块级 import 都在 import 期跑），但把 import 夹在数据之后
# 违反本仓 `pyproject.toml` 自己的 lint 契约，读的时候也分不清"哪些是依赖、哪些是数据"。
# 上移安全：`agent.workflows` 的导入者只有 `eval/` 与 `tests/`，无循环导入。
#
# 各条目为什么长这样：
#   · agent.adminops —— 写操作的纯函数层（归一/渲染，见 instantiate_plan 写分支）。
#   · agent.authz —— 管理读工具清单从它的 scope 表**派生**（20260924）：哪些工具是
#     "后台读面"是安全边界的事实，边界定义在 authz（`SCOPE_ADMIN_CONSOLE` = Rust
#     auth_guard 后面的只读接口），这里只消费它——手抄一份名单必然与边界漂移。
#     authz 只依赖 principal，无循环导入。
#   · agent.principal —— 技能可见性按角色过滤（20260921 管理助手；20260926 起是
#     **管理员族**，超管同权；20261002 加 CHAT_ONLY_ROLES 的否决支，见 visible_workflows）。
#   · agent.refs —— 参数引用 `$tool[0].field`（20260919，见 instantiate_plan）。
#   · tools.base —— 常量与判据的单一事实来源，这里只消费、不重写：
#       IOT_ENABLED / IOT_NAV_PATH：IoT 开关与入口路径与工具层**同源**（路径字面量的
#         唯一出处是 tools/base.py，那里的白名单要按同一个开关收口）；开关值本身出自
#         `config/settings.py` 的 `iot_enabled`。这里只做转发，不重新判断。
#       _NAV_EXACT_PATHS / nav_path_valid：白名单路径（/category/*、/article/* 为前缀
#         匹配，需至少带一个 id 段）。20261008 起判据本身也收进工具层一个函数——
#         这一层与工具层**各写一遍同一个条件**，此前正是它让"带定位参数的路径"两处
#         口径分岔（见该函数头注）。
#       _TODO_TEXT_LIMIT：待办正文上限，与 Rust `MAX_TEXT_CHARS` 同源。
#       _NOTICE_CONTENT_LIMIT / _NOTICE_TITLE_LIMIT / _QUOTA_NOTE_LIMIT：通知的标题/
#         正文上限，与 Rust `notice.rs` 的 `TITLE_MAX` / `CONTENT_MAX` 同源。
#   后两组在展开层挡住超长只是为了"零写 + 说清原因"，真正的硬闸在工具与服务端那两道。
import agent.adminops as A
import agent.authz as authz
from agent.principal import ADMIN_ROLES
from agent.refs import is_ref
# 下沉层（20261017 批 2 刀 2a）：数据模型与共享原语。本模块**继续按原名转发**
# （`from agent.workflows import Workflow` 这类既有写法因此逐字不动）——转发是
# **迁移期的临时形态**，批 2 收尾时本模块整块消失，54 个 import 方各自改指新模块。
from agent.workflow_model import (
    _EXPLICIT_TOOLS_MARK,
    _NO_DEFAULT,
    _PARAM_TOOLS_MARK,
    DROP_SUFFIX_BAD_ARGS,
    DROP_SUFFIX_NOT_OBJECT,
    DROP_SUFFIX_WORKFLOW_NO_CALLS,
    ParamSpec,
    Workflow,
)
from tools.base import (
    _NOTICE_CONTENT_LIMIT,
    _NOTICE_TITLE_LIMIT,
    _QUOTA_NOTE_LIMIT,
    _TODO_TEXT_LIMIT,
    IOT_ENABLED,
    IOT_NAV_PATH,
    nav_path_valid,
)
# 参数 schema 层（20261017 批 2 刀 2c）：本模块仍**消费**其中六个（实例化分支、
# 菜单渲染、别名归一），其余是**专为转发**而导入——全仓既有写法
# （`from agent.workflows import check_workflow_params` 等）因此逐字不动。
from agent.plan_status import (  # noqa: F401
    PLAN_STATUS_ABSENCE_EXEMPT,
    PLAN_STATUS_NAV_NOTE,
    PLAN_STATUS_VALUES,
)
from agent.workflow_params import (  # noqa: F401
    _bad_choice,
    _coerce_value,
    _contains_ref,
    _PARAM_ALIASES,
    _param_alias_fix,
    _param_problem_plan,
    _template_param_map,
    _TOOL_ARG_SCHEMAS,
    _TOOL_ARG_SCHEMAS_LOCK,
    arg_enum,
    arg_type_short,
    args_are_bound,
    check_call_args,
    check_workflow_params,
    expand_template_args,
    param_problem_note,
    TOOL_ARG_TYPE_SHORT,
    tool_arg_schemas,
    workflow_param_specs,
)
# 注册表字面量层（20261017 批 2 刀 2b）：47 条 `Workflow(...)` 按域搬进
# `agent/workflow_defs/`，派生件（靶通道表、四个桶、写名单、可见性判据）随注册表走，
# 导航映射表与能力否定词表各随其消费面走。本模块**继续按原名转发**
# （`from agent.workflows import NAV_MAP` 这类既有写法因此逐字不动）——转发是
# **迁移期的临时形态**，批 2 收尾时本模块整块消失，各 import 方各自改指新模块。
# 三处带 `noqa: F401` 的 import 块是"这些名字**专为转发**而导入"的标记：本模块不是它们
# 的消费方，而是别人 import 本模块的入口——那三行随本模块一起消失，别把 noqa 抄去别处。
# （这里**不能**把那个标记原样写出来：一行里出现 `noqa:` 加非代码的尾巴，ruff 会判
# "无效的 noqa 指令"，于是**那一行的抑制整条失效**——20261017 实测踩过。）
from agent.workflow_defs import (  # noqa: F401
    _channel,
    _derive_write_workflow_names,
    _FREE_TEXT_WRITE_WORKFLOWS,
    _IOT_ONLY_WORKFLOWS,
    _LEDGER_TARGET_WRITE_WORKFLOWS,
    _OWN_WRITE_WORKFLOWS,
    _SYSTEM_ONLY_WORKFLOWS,
    _WRITE_NAME_TARGET_WORKFLOWS,
    _WRITE_SCOPES,
    _WRITE_TARGET_CHANNEL,
    WORKFLOW_MAP,
    WORKFLOWS,
    WRITE_WORKFLOW_NAMES,
    visible_workflows,
)
from agent.workflow_defs.capability import (  # noqa: F401
    _NEAR_MISS_CONTRACT,
    _ROSTER_CHECK_TAIL,
    CAPABILITY_DENIAL_OBJECTS,
    CAPABILITY_DENIAL_VERBS,
)
from agent.workflow_defs.nav import (  # noqa: F401
    _IOT_NAV_ALIASES,
    _IOT_OFF_NOTE,
    _iot_alias_entries,
    _NAV_REAL_PAGES,
    _NAV_REF_HINT,
    _PUBLIC_NAME_TAKEN,
    DASHBOARD_PANELS,
    FUZZY_NAV_RULES,
    fuzzy_nav_hit,
    NAV_MAP,
    NAV_VALID_PATHS,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 按名字写数据的共同转述契约（20260925）
# ---------------------------------------------------------------------------
# 已下沉到 `agent/workflow_defs/capability.py`（20261017 批 2 刀 2b）：它只被注册表字面量
# 引用，而字面量现在按域住在那六个模块里。本模块顶部 import 回来（迁移期转发）。

# ---------------------------------------------------------------------------
# 导航映射表（业务唯一数据源）——已下沉到 `agent/workflow_defs/nav.py`（批 2 刀 2b）
# ---------------------------------------------------------------------------
# 别名表 / IoT 别名与 `_IOT_OFF_NOTE` / `_NAV_REF_HINT` / `_NAV_REAL_PAGES` / 后台面板表 /
# `_PUBLIC_NAME_TAKEN` / `NAV_VALID_PATHS` / 口语模糊规则与 `fuzzy_nav_hit` 全在那里。
# 搬走的理由（顺序是契约、判据只能有一处）见该模块头注；本模块顶部 import 回来。


# ── 写技能的**靶通道**与四个桶 —— 已下沉到 `agent/workflow_defs/__init__.py`（批 2 刀 2b）
# `_WRITE_TARGET_CHANNEL` / `_channel` / `_WRITE_NAME_TARGET_WORKFLOWS` /
# `_LEDGER_TARGET_WRITE_WORKFLOWS` / `_OWN_WRITE_WORKFLOWS` / `_FREE_TEXT_WRITE_WORKFLOWS`
# 都跟着注册表走了（它们只读 `WORKFLOWS` 那一张表）。本模块顶部 import 回来（迁移期转发）。
# ── 能力否定判据词（20261003 洞⑫）—— 已下沉到 `agent/workflow_defs/capability.py`（批 2 刀 2b）
# `CAPABILITY_DENIAL_OBJECTS` / `CAPABILITY_DENIAL_VERBS` 只被注册表字面量引用，跟着走了。
# 本模块顶部 import 回来（迁移期转发）。



def _norm_pos_int(value) -> int | None:
    """实参 → 正整数；否则 None（缺参守卫用，与 tools.base._as_article_id 同判据）。"""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    s = str(value).strip()
    return int(s) if s.isdigit() and int(s) > 0 else None


# 数字清单的**定格式**（见 `_pos_int_list`）：剥掉成对的括号/引号，再按逗号/顿号/
# 分号/斜杠/空白切分。这两条是**标点级**的封闭类，不是"中文词表"——它们拆的是
# **固定格式**的文本，与 `agent/provenance.py::parse_evidence` 收五种形状是同一条
# 纪律（形状漂移的代价不该由主人承担）。挂在变量上而不是塞进集合字面量里，也顺带
# 不惊动 `eval/wordlist_gate.py` 的穷举量（它数的是"住在集合里的中文串"，标点不算）。
_LIST_WRAP = "[](){}<>（）【】「」『』“”\"'` \t\r\n"
_LIST_BREAK = ",;|/、，；\t \r\n"


def _pos_int_list(value) -> tuple[list[int], list[str]]:
    """实参 → （**去重保序**的正整数表，**认不出的原样片段**）。

    多篇形态用（`article_status.article_ids`）。两类实参都收：真数组，或**一段写着
    清单的文本**——后者是模型交上来的常见形状（20261015 生产 trace
    `20261015T080030` 实证：`article_ids` 填成 `"[54, 13, 11, 10]"`），从前一律丢掉
    ⇒ 只算 `article_id` 那一篇、其余几篇**不留任何痕迹**（20261009「剩下的篇不留
    痕迹」换了一张脸）。文本形态按上面的**定格式**解析，认不出的片段一个都不猜。

    **认不出的必须回传给调用方**：旧签名两种情况都返回空表，而"没给"与"给的东西
    一个都认不出"**不是同一件事**——调用方那条 `not aids` 守卫会把后一种也当成"缺
    id"，于是多篇请求静默降级成单篇。回传片段让调用方**响亮地零工具问清**（同
    `_norm_pos_int` 的 None：同一个语义只有一种表示，这是 20260928 那条教训的另一半）。
    """
    if isinstance(value, (list, tuple)):
        out: list[int] = []
        for x in value:
            n = _norm_pos_int(x)
            if n is not None and n not in out:
                out.append(n)
        return out, []
    if isinstance(value, bool) or value is None:
        return [], []
    text = str(value).strip()
    if not text:
        return [], []
    flat = text.strip(_LIST_WRAP).strip()
    one = _norm_pos_int(flat)
    if one is not None:
        return [one], []          # 单个值（`"22"` / `"[22]"`）——与旧行为逐字节同
    for ch in _LIST_BREAK:
        flat = flat.replace(ch, " ")
    out2: list[int] = []
    bad: list[str] = []
    for piece in flat.split():
        n = _norm_pos_int(piece.strip(_LIST_WRAP))
        if n is None:
            if piece not in bad:
                bad.append(piece[:20])
        elif n not in out2:
            out2.append(n)
    return out2, bad

# `NAV_VALID_PATHS` 已随导航映射表搬到 `agent/workflow_defs/nav.py`（批 2 刀 2b）；
# 本模块顶部 import 回来（迁移期转发）。

# planner 可显式点名的无参只读工具白名单（20260902 用户拍板）：留言/说说/公告/
# 时间/站点信息类查询是"一次简单工具调用、无流程"，不成技能——planner 直接
# PARAMS.tools 点名，instantiate_plan 白名单校验后展开进 TOOLS 行由 execute
# 确定性执行。仅限无参只读工具（带参检索走下方 _CALLABLE_QUERY_TOOLS 的
# PARAMS.calls 通道）。
# 20260913 补齐站点信息/列表类（get_blog_info/get_social_links/get_site_map/
# get_top_notes/list_categories/list_tags）：此前 22 个注册工具里 9 个 planner
# 结构上够不到，"作者社交链接/ICP 备案号"这类问题没有数据工具可点名——planner
# 只能拿 rag_search/search_notes 去绕，而检索索引只有文章正文、对站点元数据零
# 命中，绕完如实答"站内没有"（20260913 15:51 trace 实证：数据一直在 /social、
# /user）。刻意不收：search_knowledge_base（/knowledge 端点返回空）、
# get_chat_history（占位实现，历史由系统注入）——工具本身不可用，进白名单只会
# 把"查不到"变成默认结局。
# 顺序 = planner 菜单展示顺序（graph._tools_desc 与下方技能描述枚举同源生成）。
_EXPLICIT_TOOLS_ORDER: list[str] = [
    "list_guestbook", "list_talks", "get_announcements", "get_current_time",
    "get_blog_info", "get_social_links", "get_site_map",
    "get_top_notes", "list_categories", "list_tags",
    # 用户自己的数据（20260923）：无参只读、以发起人身份读他自己的那一份
    # （scope=read.own，见 agent/authz.py）。进菜单是刻意的——"我收藏了哪些文章/
    # 有没有未读公告"是访客最常见的自指提问，没有这条路时 planner 只能拿
    # search_notes/rag_search 去绕，而检索索引里**没有**"谁收藏了什么"
    # （收藏是用户私有关系，不在文章正文里），绕完只会如实答"站内没有"。
    # 匿名用户同样看得到这三项（菜单是角色无关常量），命中时由工具层如实说未登录。
    "list_my_favorites", "get_unread_summary", "list_notifications",
    # 自己的信箱（20260923 批 8）：同一族——"我信箱里有谁给我写过信/我发的信"
    # 是自指提问，而检索索引里没有"谁给谁写过信"（私信是私有关系，不在正文里）。
    # ⚠️ 站内信（私信）≠ 河灯留言：后者是公开页面上谁都看得见的内容（list_guestbook）。
    "list_my_messages",
    # 自己放过的河灯（20261008）：同一族的**第三种自指**——「我哪条留言通过了 /
    # 我放过的灯有哪些」。公开池（list_guestbook）读不到这一问：它恒只放行**已通过**
    # 的灯、且不带"哪条是你自己放的"（上游 `mine` 要令牌才算得出来，而那是公开接口）
    # ⇒ 没有这一条时 planner 只能拿 list_guestbook 空转。trace `20261008T024742`
    # 现场：主人「带我去看看我已经通过的留言」→ `list_guestbook()` 连点两次 →
    # `data_repeat` 拦截 → 收尾轮编出"已经带你过去了"（gate 的 `nav_arrival_no_frame`
    # 拦下，最终回了句"我收回上一条"，等于这件事没办成）。
    "list_my_board",
]
_EXPLICIT_TOOLS: set[str] = set(_EXPLICIT_TOOLS_ORDER)

# planner 可带参点名的查询工具白名单（20260903 planner 全权裁决）：知识型/验证型
# 问题的调用清单（PARAMS.calls）仅限这些只读工具——检索定位（search_notes 关键词 /
# rag_search 相关度）、读全文（get_article_detail）与数据直取。动作工具（navigate_to/
# device_oled_display 等）不在任何 planner 白名单内，只能由技能模板展开——planner
# 无法通过 calls 通道越权动作。顺序 = 菜单展示顺序（同上）。
_CALLABLE_QUERY_TOOLS_ORDER: list[str] = _EXPLICIT_TOOLS_ORDER + [
    "search_notes", "rag_search", "get_article_detail", "list_notes", "get_weather",
]
_CALLABLE_QUERY_TOOLS: set[str] = set(_CALLABLE_QUERY_TOOLS_ORDER)

# 后台读面工具（20260924 用户拍板：**按角色**并入可点名清单）——从 authz 的 scope 表
# 派生（见顶部 import 注），顺序即 authz 里的登记顺序（按报表分组，稳定）。
_ADMIN_QUERY_TOOLS_ORDER: list[str] = [
    name for name, scope in authz.TOOL_SCOPE.items()
    if scope == authz.SCOPE_ADMIN_CONSOLE
]


def _admin_extra(flat: list[str]) -> list[str]:
    """后台读面里**不在** `flat` 的那些（按 authz 登记顺序）——两条通道共用。"""
    return [t for t in _ADMIN_QUERY_TOOLS_ORDER if t not in flat]


def callable_query_tools(role: str | None) -> list[str]:
    """本轮角色可点名的工具清单（顺序 = planner 菜单展示顺序，= `PARAMS.calls` 的可用集合）。

    公开那半是角色无关常量（`_CALLABLE_QUERY_TOOLS_ORDER`）；**管理员**额外拿到
    `_ADMIN_QUERY_TOOLS_ORDER`（后台读面，scope=admin.console）。

    **为什么按角色放开**（20260924 实测的代价）：那 5 个后台读工具本来只有
    `admin_notes`/`user_report`/… **技能通道**一条路可达，而工具菜单是角色无关常量
    ⇒ 管理员 planner 看到菜单上有 `list_admin_notes` 就点名它 ⇒ 被剔空 ⇒ 触发剔空
    纠偏重决策 ⇒ 下一轮再点一次。实测 `admin_notes_console_list` **每次跑满 4 个
    规划轮、同一工具执行 4 次**（四倍 token、四倍上游查询）——"意图对路但通道不对"
    的账单。

    **两条通道都要按角色取**（同日第二版，被 golden 实证打回）：planner 看菜单上
    写的是 `- list_admin_notes()`（无参），自然写进 **`PARAMS.tools`**（无参点名），
    而 `tools` 那条通道此前仍是角色无关常量 ⇒ 照样剔空、照样 4 轮（trace
    `20260924T055910` 实证）。所以 `tools` 的可用集合由 `explicit_tools(role)` 给、
    `calls` 的由本函数给，**两条同源同角色**。

    **写面工具永远不在清单里**（它们经技能模板展开，写操作必须过"本轮明确下令"
    那道门）；本函数只加 `SCOPE_ADMIN_CONSOLE`（只读）那一族，`test_workflows` 另有
    断言把这条锁死——清单里出现任何写 scope 的工具即红。

    `role=None`（身份不明/单测/老路径）→ 只剩公开清单，**失败取向往保守一侧倒**
    （与 `visible_workflows`、authz 同向）。
    """
    if role in ADMIN_ROLES:
        return _CALLABLE_QUERY_TOOLS_ORDER + _admin_extra(_CALLABLE_QUERY_TOOLS_ORDER)
    return list(_CALLABLE_QUERY_TOOLS_ORDER)


def explicit_tools(role: str | None) -> list[str]:
    """本轮角色可经 `PARAMS.tools` 点名的**无参只读**工具（顺序同上）。

    与 `callable_query_tools` 的差别是"这条通道只收无参的"：后台读面那 5 件都
    可以无参调用（`get_moderation_status` 的 `status` 是可选过滤），所以整族在
    管理员档进本通道，同时在 `param_tools(admin)` 里也有——带过滤参数的写法走
    `PARAMS.calls` 同样合法，**两条写着都一样**，planner 不必猜哪条才对。
    """
    if role in ADMIN_ROLES:
        return _EXPLICIT_TOOLS_ORDER + _admin_extra(_EXPLICIT_TOOLS_ORDER)
    return list(_EXPLICIT_TOOLS_ORDER)


def param_tools(role: str | None) -> list[str]:
    """`PARAMS.calls` 描述文本里列出的工具（= 调用清单减去无参点名那半）。

    只为**描述文本**服务（白名单判据在 `callable_query_tools`，别拿它当判据）。
    管理员档把后台读面整族也列上——它们带过滤参数（如 `get_moderation_status` 的
    `status`）时只能走本通道。
    """
    public = [t for t in _CALLABLE_QUERY_TOOLS_ORDER if t not in _EXPLICIT_TOOLS]
    if role in ADMIN_ROLES:
        return public + list(_ADMIN_QUERY_TOOLS_ORDER)
    return public


# 技能描述里的工具枚举标记（20260924）：枚举**不能是模块常量**（角色相关，得在
# build_planner_context 里按 role 展开）。两个标记本体已下沉到
# `agent/workflow_model.py`（20261017 批 2 刀 2a）——注册表字面量与菜单渲染器都要
# 看得见它们，而两边谁也不该 import 谁；本模块顶部已 import 回来，行为逐字不变。


def render_tool_marks(text: str, role: str | None) -> str:
    """把技能描述里的工具枚举标记按本轮角色展开（`build_planner_context` 调）。"""
    return (text.replace(_EXPLICIT_TOOLS_MARK, "/".join(explicit_tools(role)))
                .replace(_PARAM_TOOLS_MARK, "/".join(param_tools(role))))


# 口语模糊归一（`FUZZY_NAV_RULES` / `fuzzy_nav_hit`）已随导航映射表搬到
# `agent/workflow_defs/nav.py`（批 2 刀 2b）；本模块顶部 import 回来（迁移期转发）。


# `Workflow`（技能的数据模型 + 只读的 `contract` 取法）已下沉到
# `agent/workflow_model.py`（20261017 批 2 刀 2a）：它被注册表字面量、参数 schema、
# 展开层、实例化、菜单五族共用，谁也不该 import 谁 ⇒ 落在共同的下沉层，
# 本模块顶部 import 回来。

# 「先用 account_roster 核对账号名」那条契约的**共用尾巴**已下沉到
# `agent/workflow_defs/capability.py`（批 2 刀 2b）；本模块顶部 import 回来。


# ---------------------------------------------------------------------------
# 技能注册表本体（47 条 `Workflow(...)` 字面量）+ 由它派生的写名单
# ---------------------------------------------------------------------------
# 已搬到 `agent/workflow_defs/`（20261017 批 2 刀 2b）：字面量按域分住六个模块，
# `WORKFLOWS` 由 `__init__.py` **保序拼回**（顺序是契约——它驱动 `WORKFLOW_MAP`、
# planner 菜单排列与 `visible_workflows` 的产出顺序，改顺序就是改提示词字节）。
# `WORKFLOW_MAP` / `_WRITE_SCOPES` / `_derive_write_workflow_names` / `WRITE_WORKFLOW_NAMES`
# 都跟着走。本模块顶部 import 回来（迁移期转发）。


# ---------------------------------------------------------------------------
# 技能模板实例化：planner 选技能 + 参数 → plan 字段（契约的写端）
# ---------------------------------------------------------------------------

def _write_arg(value) -> str:
    """写技能参数值 → 字符串（**不做类型强转、不吞引用**）。

    20260921 事故的核心就在这一步：旧代码用 `_norm_pos_int(params.get("parent_id"))`
    把 `$list_tags[3].tagId` 变成 `None` ⇒ 参数**静默消失** ⇒ 注记还肯定地写下
    「（一级标签）」。现在：空 → 空串（调用方剔掉）；其余**原样字符串化**——
    引用字面量照原样进 TOOLS 行，交给 execute 的 `resolve_args` 解析；解析不出来
    会产带原因码的 `__ERROR__` 帧（响亮、零执行），解析得出来就是真取到了值。
    两条路都比"悄悄当你没填"好。
    """
    if value is None:
        return ""
    if isinstance(value, bool):
        # 这几个写技能的参数没有布尔语义（"有没有填"由键是否存在表达，不由值表达）。
        # 落成 "True" 会让工具收到一个名叫「True」的标签名——当没填更接近实情。
        return ""
    return str(value).strip()


def _write_list_arg(value) -> list[str]:
    """写技能的**数组**参数值 → 值清单（保序、去重、去空）。

    与 `_write_arg` 同一套取向（不做类型强转、不吞引用、宁可少说），差在收的是数组：
      · 单个字符串也认——文本档 planner 把 `titles: ["Git","SVN"]` 写成
        `titles: "Git"` 是同一层意思的另一种写法，不是错误（判它错只会白烧一轮）；
      · 对象元素里认得出 `title` / `name` 就取出那个字符串（原生档的 schema 把
        items 收成 string，文本档收不紧——同族形态在别的槽位实测出现过）；
      · 认不出的元素**丢掉**。它不可能是主人说的名字，而让一个 `{'a': 1}` 变成
        标签名比少建一个糟得多；少掉的那一个在确认卡上看得到（卡面逐条印名字），
        主人核对时就能纠正——所以这条不是静默的。
    """
    items = value if isinstance(value, list) else [value]
    out: list[str] = []
    for item in items:
        if isinstance(item, dict):
            item = item.get("title") or item.get("name")
        if isinstance(item, list) or isinstance(item, dict):
            continue
        s = _write_arg(item)
        if s and s not in out:
            out.append(s)
    return out


def _tag_names(params: dict) -> list[str]:
    """`tag_create` 的目标名字清单：`title`（单数槽位）与 `titles`（复数槽位）合并。

    两个槽位**都读**、而不是二选一：老语料里"建一个"一直填 `title`，新开的 `titles`
    只该在主人一口气点了好几个名字时出现，但两格同时有值是**合法输入**（原话里既
    点了一个名字又列了一串）——合并去重才是主人真正说的那一批。顺序先 `title`
    后 `titles`：TOOLS 行与卡面都按这个顺序印，主人核对时对得上自己的话。
    """
    out = _write_list_arg(params.get("title"))
    for one in _write_list_arg(params.get("titles")):
        if one not in out:
            out.append(one)
    return out


_TEMPLATE_TOKEN_RE = re.compile(r"^\$[A-Za-z_][A-Za-z0-9_]*$")


def _unfilled_placeholder(params: dict) -> tuple[str, str] | None:
    """参数值**就是技能模板自己的占位符**时返回 `(键, 值)`，否则 None（20260928）。

    技能 `plan` 模板里的参数位写的是 `$name` / `$title` 这种**填充记号**（由
    `expand_template_args` 拿同名参数替换，见它的头注）——它**不是**一个可以照抄的值。
    模型偶尔会把模板整行原样搬进 PARAMS（全库 trace 仅 1 例：`{"name": "$name"}`，
    trace `20260927_235721` 的 `account_freeze`），而那一例真的把「冻结账号「$name」」
    印上了确认卡：主人唯一能核对的那行字是个记号，他一点下去，动的就是一个活人的
    登录能力。所以判据必须在**展开这一层**拦下（零工具 + 交回 planner 重决策），
    而不是等卡片印出来才靠人眼发现。

    只认两个**机器能保证**的形态，不判"填得好不好"（那一维靠弹卡给人眼看）：
      ① 值恰好是 `$` + **它自己这个参数的键名**（模板照抄，实测的唯一形态）；
      ② 值是一个**裸的 `$词`**（`_TEMPLATE_TOKEN_RE`）——模板记号换了个键名抄进来。
    正常的主人原话里取到的名字不会长这样；万一真撞上（有人就管一个标签叫 `$foo`），
    代价是主人被反问一句、而那句话本来办得成——比"印一张记号卡让他盲签"小得多。

    与**参数引用**（`$tool[0].field`）不冲突：引用必须带方括号（`agent/refs.py` 的
    `REF_RE`），撞不上这两个形态。那一层由 `check_workflow_params` / `resolve_args` 管，
    本函数不碰。
    """
    if not isinstance(params, dict):
        return None
    for key, val in params.items():
        # **数组元素逐项看**（20261008，`tag_create.titles` 新开的复数槽位）：元素
        # 恰好是模板记号时的形状与单数槽位一模一样（`{"titles": ["$titles"]}`），
        # 而下面那层写展开会把每一项都变成一次真写——只看顶层 str 会整格漏过去，
        # 印出一张写着记号的卡，主人一点，站里就多一个叫 `$titles` 的标签。
        for one in (val if isinstance(val, list) else [val]):
            if not isinstance(one, str):
                continue
            tok = one.strip()
            if tok == "$" + str(key) or _TEMPLATE_TOKEN_RE.match(tok):
                return str(key), tok
    return None


_PLACEHOLDER_WRAPS = (("[" , "]"), ("【", "】"), ("（", "）"), ("(", ")"), ("{", "}"),
                      ("<", ">"), ("「", "」"), ("『", "』"), ('"', '"'), ("'", "'"))


def _notice_placeholder(text: str) -> str:
    """通知正文看起来是**占位符**时给一句原因，否则空串（20260926 真机实测）。

    只认两种**机器能保证**的形态，不判"整理得好不好"（那一维靠弹卡给人眼）：
      ① 整串就是一个工具名或技能名（实测出现过正文＝`notice_send`——模型把技能名
         填进了正文槽）；
      ② 整串被一对括号/引号包住（实测出现过正文＝`[当前登录用户]`——模板占位符）。
    正常的人类句子（含"你好，这是一条测试通知。"这种改写过的一句）**一个都不拦**：
    拦错的代价是主人被反问一句、而那句话本来办得成。
    """
    s = (text or "").strip()
    if not s:
        return ""
    low = s.lower()
    try:
        from tools.base import get_all_tools
        tool_names = {getattr(t, "name", "") for t in get_all_tools()}
    except Exception:            # 注册表取不到就只查技能名（不因为取不到而放行①）
        tool_names = set()
    if low in {n.lower() for n in tool_names if n} or s in WORKFLOW_MAP:
        return f"正文是工具名/技能名「{s}」"
    for open_c, close_c in _PLACEHOLDER_WRAPS:
        if (len(s) <= 44 and s.startswith(open_c) and s.endswith(close_c)
                and (open_c == close_c or s.count(open_c) == 1)):
            return f"正文整个被括号包住「{s}」"
    return ""


def _expand_change_set(workflow, params: dict) -> tuple[list[str], str]:
    """**变更集**技能（`review_inbox`）的展开：`params["calls"]` 逐条照抄成调用清单。

    与其余展开函数的根本差别：**这里没有"解释"这一步**。`calls` 里的每一条已经是
    「工具 + 参数」的完整调用（形状与 `content_query.calls` **逐字同构**：模型侧由
    `planner_llm._SCHEMA_OVERRIDES[("review_inbox","calls")]` 给出闭集）——这一层与
    它之间是一条**恒等**通道：不补参、不改写、不排序、不合并。理由是同一条纪律的
    另一面："参数不全时猜一个"在别处只是办不成，在这一族是**拿别人的一句话做裁决**。

    两条形状判据（缺一即**零工具 + 注记**，交 planner 如实收尾）：
      · `calls` 必须是非空列表，每条是 `{"tool": 名字, "args": 非空对象}`；
      · 工具名必须在本技能 `plan` 声明的**全集**里——与 `_confirm_grant_plan` 执行轮
        那条"技能与工具必须对得上"是同一条判据，在这里**先判一次**的理由是：展开
        结果一旦含 plan 之外的工具，令牌签发（按技能名）与执行（按 plan 逐条核对）
        会得出不同结论，而"写操作跑在一份没人预期它会跑的技能名下"是最难查的那类事故。
        这一条同时是**跨族安全性的来源**：模型能在 `calls` 里写的工具名被收在这两件里，
        别的写工具（删留言 / 重置额度 / 发通知…）拼不进来。

    **任何一条不合格 → 整批零工具**（不是"跳过坏的那条"）：卡是为这一批签的，
    少办一件而主人以为全办了，比一件都不办更坏（同 `_drop_correction` 的剔空取向）。
    """
    allowed = {t for t, _ in workflow.plan}
    calls = params.get("calls")
    if not isinstance(calls, list) or not calls:
        return [], ("变更集里没有一条可执行的调用（`calls` 是空的）："
                    "不调用任何工具，如实告诉主人这一轮没有能替他办的事")
    tools: list[str] = []
    bad: list[str] = []
    for s in calls:
        if not isinstance(s, dict):
            bad.append("（有条目不是对象）")
            continue
        name = str(s.get("tool") or "").strip()
        args = s.get("args")
        if not name or not isinstance(args, dict) or not args:
            bad.append(f"「{name or '没有工具名'}」缺参数")
            continue
        if name not in allowed:
            bad.append(f"「{name}」不在本技能的允许清单里")
            continue
        # 调用行的写法是**跨模块契约**：`<工具名>(<JSON>)`，读端是
        # `graph._tool_name` / `_tool_args` 与 `_confirm_grant_plan`（它拼的是同一形状）。
        tools.append(f"{name}({json.dumps(args, ensure_ascii=False)})")
    if bad or not tools:
        return [], ("变更集里有条目不合格（" + "、".join(bad[:5])
                    + "）：不调用任何工具，如实告诉主人这一轮什么都没有办")
    return tools, f"变更集：按清单逐条执行（共 {len(tools)} 件）"


def _expand_write_workflow(workflow, params: dict) -> tuple[list[str], str]:
    """写技能（WRITE_WORKFLOW_NAMES）模板 + 参数 → (TOOLS 行清单, 注记)。

    注记非空**且 tools 为空** = "参数不齐/认不出"的零工具路径（由 planner 去追问）；
    注记非空且有 tools = 正常路径的说明。写成函数是因为这条分支已经装不下：
    八个写技能共用同一套纪律（缺参零工具 / 只落点名的参数 / 归一在确定性层做）。

    **目标一律按名字**（20260921 第四轮）：planner 写「编程」而不是 id——用户嘴里
    说的就是名字，跨轮执行记忆里也只有名字没有编号；名字→id 的解析在工具侧
    确定性完成（`tools.base._find_named_tag`），对不上就零写 + 如实说明。
    """
    name = workflow.name
    tools: list[str] = []
    note = ""

    def _spec(tool_name: str, args: dict) -> str:
        return f"{tool_name}({json.dumps(args, ensure_ascii=False)})"

    if name == "tag_create":
        names = _tag_names(params)
        if not names:
            return [], ("tag_create 缺少标签名（title）：不调用任何工具，"
                        "如实向主人问清要建的标签叫什么名字")
        # 颜色（20260921）：点名了就**在这里解析成站内色板 hex**——确定性、且只有
        # 这一处知道色板；执行轮（含确认轮）拿到的就是色值，弹窗问句与工具参数都
        # 从同一个值渲染。点名的色认不出 → 零工具 + 注记（**绝不回落到哈希**）。
        color_spec = _write_arg(params.get("color"))
        hexval = A.match_tag_color(color_spec) if color_spec else None
        if color_spec and hexval is None:
            return [], (f"color「{color_spec}」不在站内色板里（可选：{A.TAG_COLOR_SPEC}）："
                        "不调用任何工具，如实向主人说明只有这几种颜色，请他挑一个")
        pname = _write_arg(params.get("parent_tag"))
        # **一名一 spec**（20261008 批：主人一次点名好几个新标签时不许只建第一个）。
        # 此前这一支恒产出一条 spec，于是"建 Git 和 SVN 两个标签"这种话在契约层就
        # 表达不出来：planner 只能挑一个填进 title，另一个在**没有任何一层**留下痕迹
        # ——实测连着两轮原型一模一样的一张单标签卡（trace `20261008T065806` /
        # `20261008T065826`），主人说"两个你漏了"也没用。现在 `titles` 与 `title`
        # 合并成 N 条同形 spec，N≥2 时下游**现成的**多 spec 通道全部自动接上：确认卡
        # 印成清单（「全部办」/「只办第 N 件」，`render_confirm_text`）、逐条执行、
        # 逐条回执。父标签与颜色是**整批共用**的（主人说「在编程下面建 A 和 B」时
        # 只有一个父标签），所以循环里带着同一份值。
        # 展开层的三条防线一个都没松：名字空 → 上面已零工具；名字已被站里占用 →
        # `adminops.reached_specs` 把**那一条**摘掉（其余照弹卡，不是整批放行）；
        # 名字在主人这句话里找不到出处 → `_ident_grounded` 逐 spec 判、至少一条没据
        # 就退成弹卡给人眼看（见 graph._confirm_popup 的循环）。
        specs: list[str] = []
        for nm in names:
            args = {"title": nm}
            if hexval:
                args["color"] = hexval
            if pname:
                args["parent_tag"] = pname
            specs.append(_spec("create_tag", args))
        # 注记只写**已知事实**：父标签名是 planner 给的，它是不是真存在由工具去核，
        # 这里**不许**替它断言层级（旧注记无条件写「（一级标签）」——参数被吞掉时
        # 那句就成了一个肯定的错误事实，落进执行记忆被下一轮照念）。
        said = f"挂在父标签「{pname}」下" if pname else "一级标签（未给父标签）"
        if len(names) > 1:
            head = ("新建标签" + "、".join(f"「{n}」" for n in names)
                    + f"（共 {len(names)} 个，{said}）")
        else:
            head = f"新建标签「{names[0]}」（{said}）"
        return specs, (
            head
            + (f"，颜色 {A.describe_color(hexval)}" if hexval else "")
            + "；同名已存在时工具会复用而不是重复建")

    if name in ("tag_update", "tag_delete"):
        target = _write_arg(params.get("name"))
        if not target:
            return [], (f"{name} 缺少标签名（name）：不调用任何工具，"
                        "如实向主人问清说的是哪一个标签")
        lv = _write_arg(params.get("level"))
        args: dict = {"name": target}
        if lv:
            code = A.normalize_level(lv)
            if code is None:
                return [], (f"level「{lv}」认不出来（只支持一级 / 二级）："
                            "不调用任何工具，如实向主人问清")
            args["level"] = code
        if name == "tag_delete":
            what = "一级" if args.get("level") == "one" else ("二级" if args.get("level") == "two" else "")
            return [_spec("delete_tag", args)], (
                f"删除{what}标签「{target}」"
                + ("。**这是不可撤销的**：一级标签会连带删掉它下面的二级标签，"
                   "并把这些标签从所有文章上摘掉" if what != "二级" else "。**不可撤销**"))
        bits = []
        new_title = _write_arg(params.get("new_title"))
        if new_title:
            args["new_title"] = new_title
            bits.append(f"改名→{new_title}")
        color_spec = _write_arg(params.get("color"))
        if color_spec:
            hexval = A.match_tag_color(color_spec)
            if hexval is None:
                return [], (f"color「{color_spec}」不在站内色板里（可选：{A.TAG_COLOR_SPEC}）："
                            "不调用任何工具，如实向主人说明只有这几种颜色，请他挑一个")
            args["color"] = hexval
            bits.append(f"颜色→{A.describe_color(hexval)}")
        pname = _write_arg(params.get("parent_tag"))
        tl = _write_arg(params.get("to_level"))
        if tl:
            code = A.normalize_level(tl)
            if code is None:
                return [], (f"to_level「{tl}」认不出来（只支持一级 / 二级）："
                            "不调用任何工具，如实向主人问清")
            args["to_level"] = code
        if pname:
            if args.get("to_level") == "one":
                return [], ("既说要挪到某个父标签下、又说要改成一级标签——两者矛盾："
                            "不调用任何工具，如实向主人问清到底要哪一种")
            args["parent_tag"] = pname
            bits.append(f"移到「{pname}」下面")
        if args.get("to_level") == "one":
            bits.append("改成一级标签")
        elif args.get("to_level") == "two":
            bits.append("改成二级标签（需同时给父标签名）")
        if not bits:
            return [], ("tag_update 没有指出要改什么（new_title / color / parent_tag / "
                        "to_level）：不调用任何工具，如实向主人问清要改成什么")
        return [_spec("update_tag", args)], f"修改标签「{target}」：{'、'.join(bits)}"

    if name in ("category_create", "category_update", "category_delete"):
        if name == "category_delete":
            cname = _write_arg(params.get("name"))
            if not cname:
                return [], ("category_delete 缺少分类名（name）：不调用任何工具，"
                            "如实向主人问清说的是哪一个分类")
            return [_spec("delete_category", {"name": cname})], (
                f"删除分类「{cname}」（文章不会被删，它们会变成没有分类）")
        if name == "category_create":
            title = _write_arg(params.get("title"))
            if not title:
                return [], ("category_create 缺少分类名（title）：不调用任何工具，"
                            "如实向主人问清要建的分类叫什么")
            args = {"title": title}
            bits = [f"新建分类「{title}」"]
        else:
            cname = _write_arg(params.get("name"))
            if not cname:
                return [], ("category_update 缺少分类名（name）：不调用任何工具，"
                            "如实向主人问清说的是哪一个分类")
            args = {"name": cname}
            bits = [f"修改分类「{cname}」"]
        color_spec = _write_arg(params.get("color"))
        if color_spec:
            # 分类用宽一档的色表（任意 6 位 hex 也收——前端色块白名单已放宽）
            picked = A.match_any_color(color_spec)
            if picked is None:
                return [], (f"color「{color_spec}」认不出来（中文色名或 6 位色值如 #eb2f96）："
                            "不调用任何工具，如实向主人问清")
            args["color"] = picked
            bits.append(f"颜色→{A.describe_color(picked) if picked in A.NEW_TAG_COLORS else picked}")
        for key in ("new_title", "path_name", "introduce", "icon"):
            val = _write_arg(params.get(key))
            if val:
                args[key] = val
                bits.append(f"{key}→{val}")
        if name == "category_update" and len(bits) == 1:
            return [], ("category_update 没有指出要改什么（new_title / path_name / "
                        "introduce / icon / color）：不调用任何工具，如实向主人问清")
        # **技能名 ≠ 工具名**：分类三件的技能名是 `category_*`、工具名是 `*_category`
        # （建标签那两件恰好同名，所以这里一旦顺手写 name 就会只有分类两件坏掉）。
        # 写错的代价不是"少做一件事"而是 execute 的"未知工具"错误帧——用户看到的
        # 是一句"创建失败"的系统报错（20260922 全量 golden 实测：admin_category_create_popup
        # 整轮 FAIL、零弹窗）。锁：test_tag_admin 的工具名 ∈ 注册表 那条。
        tool_name = "create_category" if name == "category_create" else "update_category"
        return [_spec(tool_name, args)], "、".join(bits)

    if name.startswith("announcement_"):
        # 公告（20260922 第五轮）：目标按**标题**指认。正文**原样透传**——这一层只做
        # "空/非空"的判断，一个字都不改。
        # ⚠️ "正文该由谁成文"（20261006 改口径）：**两种情形分开**——主人明确给了原文
        # （「正文写：…」）⇒ 照录，一个字的改动都算改了他的意思（由
        # `graph._announcement_text_fix` 校正回原话）；只给了意思（「发个公告祝大家
        # 国庆快乐，以你的口吻」）⇒ **由 planner 按他的意思组织措辞**，这一层原样放行。
        # 这里原先写的是"正文原样透传、一个字都不改……替主人润色等于替他发言"，
        # 那句话**把只给意思的那种也一起禁了**：现场代价见 trace `20261001T061023`
        # ——主人点名要"以你的口吻"，模型照旧只回声一句「祝大家国庆节快乐！」，
        # 主人下一句就是「太干巴了，而且我要求以你的身份」。
        title = _write_arg(params.get("title"))
        if not title:
            return [], (f"{name} 缺少公告标题（title）：不调用任何工具，"
                        "如实向主人问清是哪一条公告（新建时问清标题叫什么）")
        if name == "announcement_delete":
            return [_spec("delete_announcement", {"title": title})], (
                f"删除公告「{title}」（删掉后首页立刻看不到，取不回来）")
        if name == "announcement_create":
            content = _write_arg(params.get("content"))
            if not content:
                # **槽位空着就不许往下走**：正文由 planner 成文（可以润色），但不能是空。
                return [], ("announcement_create 缺少公告正文（content）：不调用任何工具，"
                            "如实向主人问清公告要写什么")
            return [_spec("create_announcement", {"title": title, "content": content})], (
                f"发布公告「{title}」（正文**全文**会印在确认卡上由主人核对，"
                "他点了确定才真发出）")
        new_title = _write_arg(params.get("new_title"))
        content = _write_arg(params.get("content"))
        if not new_title and not content:
            return [], ("announcement_update 没有指出要改什么（new_title / content）："
                        "不调用任何工具，如实向主人问清要改标题还是正文")
        args: dict = {"title": title}
        bits = []
        if new_title:
            args["new_title"] = new_title
            bits.append(f"改名为「{new_title}」")
        if content:
            args["content"] = content
            bits.append("改正文")
        return [_spec("update_announcement", args)], f"修改公告「{title}」：{'、'.join(bits)}"

    if name == "board_audit":
        # 河灯留言复核（20260929 批 H 起按 **talkId**）：编号是系统给的、模型只负责
        # 原样抄 ⇒ 这里只做**归一**（`talkId:101` / `"101"` / `101` / `#101` 都收），
        # 解不出就零工具 + 追问。行是否存在由 `graph._ledger_target_refusal` 现场读
        # 台账判（比"这段字面出自主人原话"更强：可验证、不可能编造）。
        # **那一行是什么状态不参与判**（20260930：撤掉了"必须还在待审"那条，主人
        # 要能驳回已经通过的留言）——"这一下是不是他要的"由人闸回答（恒弹卡）。
        tid = A.normalize_target_id(params.get("talk_id"))
        if tid is None:
            return [], (f"board_audit 缺少指认用的留言编号（talk_id，给的是"
                        f"「{_write_arg(params.get('talk_id')) or '空'}」）：不调用任何"
                        "工具，如实向主人说明要先把那条留言的编号读出来"
                        "（台账帧或审核状况明细里印着 `talkId:<编号>`，原样抄那个数字）")
        verdict = A.normalize_verdict(params.get("verdict"))
        if verdict is None:
            return [], (f"verdict「{params.get('verdict')}」认不出来（只能是通过/pass "
                        "或驳回/reject）：不调用任何工具，如实向主人问清要放行还是驳回")
        return ([_spec("audit_board_comment", {"talk_id": tid, "verdict": verdict})],
                (f"把留言 talkId:{tid} 人工复核为{A.BOARD_VERDICT_CN[verdict]}"
                 "（卡面会印出那条留言的原文与作者，由主人核对）"))

    if name == "board_delete":
        # 河灯留言删除（20260922 第六轮）：**目标仍按正文片段**指认——主人指一条
        # 要删的留言时说的是那句内容（"把那条写广告的删了"），不是编号；改按编号会
        # 逼他多报一个数字，而正文片段撞车时工具侧零写、不会选错。审核那件不同：
        # 它要记的是"哪一行、改成了什么结论"，结论必须落在编号上才不会串行。
        # quote 原样透传——它是模型从留言原文里抄的一段，**不许在这里改写/截断**：
        # 片段越碎越容易撞到别的留言（工具侧命中多条会零写，不会选错）。
        quote = _write_arg(params.get("quote"))
        if not quote:
            return [], (f"{name} 缺少指认用的正文片段（quote）：不调用任何工具，"
                        "如实向主人问清说的是哪一条留言（或先去后台审核状况里把"
                        "那几条列出来让他指认）")
        return [_spec("delete_board_comment", {"quote": quote})], (
            f"删除含「{A.clip(quote, 20)}」的那条河灯留言（删掉取不回来）")

    if name in ("account_freeze", "account_unfreeze"):
        # 账号冻结 / 解冻（20260926 第九轮）。它与上面那几件同族（目标是一个
        # **名字**、解析在工具侧对着实时名录做），但两条注记的措辞要更硬一档：
        # 这一族说错话的代价是**把一个活人踢下线**（不是"标签建歪了"）。
        #   · 缺名字 ⇒ 明写"不要拿你猜的名字顶上"——这一族唯一的定位方式就是名字；
        #   · 纯数字 ⇒ 后台账号列表里只有名字，没有编号可写（工具**没有** user_id
        #     参数：列表不列超管那一行，那条防线只在"定位必须经过列表"时成立）。
        # 方向由**技能名**定死而不是参数：`frozen: bool` 那种参数在
        # `graph._confirm_grant_plan` 的"工具 ⊆ 技能 plan"判据下看不见——卡上写
        # 「冻结」、实际执行解冻，且无人可见。
        target = _write_arg(params.get("name"))
        if not target:
            return [], (f"{name} 缺少账号名（name）：不调用任何工具，"
                        "如实向主人问清要动的是哪一个后台账号——**不要**拿你猜的名字顶上，"
                        "也不要用账号编号")
        if target.isdigit():
            return [], (f"{name} 给的是一串数字「{target}」：不调用任何工具，"
                        "如实向主人问清那个账号的**名字**（后台账号列表里看得见的那一行；"
                        "系统不支持按编号操作账号）")
        is_freeze = name == "account_freeze"
        return ([_spec("freeze_account" if is_freeze else "unfreeze_account",
                       {"name": target})],
                (f"{'冻结' if is_freeze else '解冻'}后台账号「{target}」"
                 "（按名字在后台账号列表里解析；名字不在列表里就不动任何数据、"
                 "如实说明）——**要动的那个名字必须能在后台账号列表里看到**"))

    if name in ("mute_account", "unmute_account"):
        # 禁言 / 解除禁言（20261002 内容风控下放给 agent）。与冻结族同走名字通道、
        # 同样两条硬注记（缺名字不许猜 / 编号通道不存在），差别在**多一个 Duration
        # 参数**（只有禁言方向有）：
        #   · **归一放在这一层，且认不出就零工具**（同 `account_set_role` 的"认不出的
        #     身份"）：主人说的是「三天」，进 TOOLS 行/进卡面的必须是同一个归一结果；
        #     归不出就把问题问回去，**绝不许静默按永久办**——那会把「禁他三天」办成
        #     永久禁言，而卡面与实际同源（都走这一份归一），两边都看不出来。
        #   · 解禁方向**没有时长这回事**：多填的 hours 一律丢掉（不是宽容，是这件事
        #     没有那个维度；带进工具会让后端收到一个它明确忽略的字段）。
        target = _write_arg(params.get("name"))
        if not target:
            return [], (f"{name} 缺少账号名（name）：不调用任何工具，"
                        "如实向主人问清要动的是哪一个后台账号——**不要**拿你猜的名字顶上，"
                        "也不要用账号编号")
        if target.isdigit():
            return [], (f"{name} 给的是一串数字「{target}」：不调用任何工具，"
                        "如实向主人问清那个账号的**名字**（后台账号列表里看得见的那一行；"
                        "系统不支持按编号操作账号）")
        if name == "unmute_account":
            return ([_spec("account_unmute", {"name": target})],
                    (f"解除后台账号「{target}」的禁言"
                     "（按名字在后台账号列表里解析；名字不在列表里就不动任何数据、"
                     "如实说明）——**要动的那个名字必须能在后台账号列表里看到**"))
        raw_hours = params.get("hours")
        ok_hours, hours_n = A.normalize_mute_hours(raw_hours)
        if not ok_hours:
            return [], (f"{name} 给的禁言时长「{A.clip(str(raw_hours or ''), 20)}」"
                        "认不出是多少小时：不调用任何工具，如实向主人问清要禁多久"
                        "（例如几个小时、几天，或者说「永久」）"
                        "——**不要**自己挑一个时长顶上，也**不要**当成永久")
        span = A.mute_span_cn(hours_n)
        args = {"name": target} if hours_n is None else {"name": target, "hours": hours_n}
        return ([_spec("account_mute", args)],
                (f"禁言后台账号「{target}」"
                 f"（期限：{span}）"
                 "（按名字在后台账号列表里解析；名字不在列表里就不动任何数据、"
                 "如实说明）——**要动的那个名字必须能在后台账号列表里看到**；"
                 "禁言不踢人下线，他照常登录浏览、只是发不出评论与留言"))

    if name == "account_set_role":
        # 变更账号身份（20261002：下放给管理员 + agent 代理）。与上面冻结族同走名字
        # 通道、同样两条硬注记（缺名字不许猜 / 编号通道不存在），差别在两处：
        #   · **多一个必填的取值参数 `role`**——它不是"要写进去的值"（那种值走
        #     `_WRITE_VALUE_FIELDS` 的逐字闸），而是一次**归一**：主人说「杂鱼」
        #     与「zako」是同一件事，而进 TOOLS 行的必须是后端认的身份码。归一放在
        #     这一层而不是工具里（工具那层也做一次，兜直接调用），是因为**卡面与
        #     TOOLS 行必须同源**：卡上写「改成杂鱼」而参数是别的东西，主人签的
        #     就不是执行的那一件。
        #   · **认不出的身份零工具**（同「缺名字」那条）：让一个认不出的字面落到
        #     后端，主人会在卡上看到一句他没说过的身份；而这里能说的是一句真话
        #     ——站内没有这个身份。
        # ⚠️ 身份**只在这里归一，权限一个字都不判**：能改谁、能改成什么由后端
        #    `authz::check_role_change` 一项判据说了算（用户拍板：agent 侧不写
        #    第二份权限表）。上面那条硬注记的方向也据此改了措辞——"管理员只能改
        #    两档"这类话**不许**写进 agent 的任何文案里：它是后端的原话，agent
        #    复述一遍就会在政策变更那天变成假话（同 `_policy_post` 那条纪律）。
        target = _write_arg(params.get("name"))
        if not target:
            return [], (f"{name} 缺少账号名（name）：不调用任何工具，"
                        "如实向主人问清要动的是哪一个后台账号——**不要**拿你猜的名字顶上，"
                        "也不要用账号编号")
        if target.isdigit():
            return [], (f"{name} 给的是一串数字「{target}」：不调用任何工具，"
                        "如实向主人问清那个账号的**名字**（后台账号列表里看得见的那一行；"
                        "系统不支持按编号操作账号）")
        want = _write_arg(params.get("role"))
        if not want:
            return [], (f"{name} 缺少目标身份（role）：不调用任何工具，"
                        "如实向主人问清要把「" + target + "」改成什么身份")
        code = A.normalize_role(want)
        if code is None:
            return [], (f"{name} 的目标身份「{want}」站内没有：不调用任何工具，"
                        "如实向主人说明没有这个身份**并请他换一个说法**"
                        "——**不要**自己挑一个身份顶上")
        return ([_spec("set_account_role", {"name": target, "role": code})],
                (f"把后台账号「{target}」的身份改成{A.role_cn(code)}"
                 "（按名字在后台账号列表里解析；名字不在列表里就不动任何数据、"
                 "如实说明）——**要动的那个名字必须能在后台账号列表里看到**"))

    if name == "notice_send":
        # 给单个账号发通知（20260926 第十一轮）。三件与上面两支不同的事：
        #   · **正文是必填的**（缺了零工具 + 问清要发什么——**绝不替主人编一句**：
        #     那是拿他的名义对第三方说话，收不回）；
        #   · 正文**允许整理**（用户拍板），所以这里只挡长度、不做"必须逐字"的校验
        #     ——那一层刻意不接（`graph._WRITE_VALUE_FIELDS` 的注写了为什么）；
        #   · `title` **可留空**（服务端会落一个系统默认标题），点名了才进 args。
        # 缺收件人名字那一路的措辞与冻结族同硬（名字是唯一通道，不许猜、不许用编号）。
        target = _write_arg(params.get("name"))
        if not target:
            return [], ("notice_send 缺少收件账号名（name）：不调用任何工具，"
                        "如实向主人问清这条通知要发给**哪一个账号**"
                        "——**不要**拿你猜的名字顶上，也不要用账号编号")
        if target.isdigit():
            return [], ("notice_send 给的是一串数字「" + target + "」：不调用任何工具，"
                        "如实向主人问清那个账号的**名字**（后台账号列表里看得见的那一行；"
                        "系统不支持按编号操作账号）")
        content = _write_arg(params.get("content"))
        if not content:
            return [], ("notice_send 缺少正文（content）：不调用任何工具，"
                        "如实向主人问清这条通知要说什么——**不要**替他写一句："
                        "那段话会以他的名义发给对方，且发出后没有撤回的通道")
        if len(content) > _NOTICE_CONTENT_LIMIT:
            return [], (f"notice_send 的正文太长（{len(content)} 字，上限 "
                        f"{_NOTICE_CONTENT_LIMIT} 字）：不调用任何工具，"
                        "如实向主人说明太长、请他说短一点")
        ph = _notice_placeholder(content)
        if ph:
            # 20260926 真机实测（本技能上线当天，10 次采样）：planner 有 2 次把正文
            # 填成了**系统自己的字**——一次 `notice_send`（技能名）、一次
            # `[当前登录用户]`（模板占位符），另有几次把主人的话整个换成一句通用语。
            # 正文是"以主人的名义发给第三方、且发出去删不掉"的那一段 ⇒ 占位符必须
            # 在**展开这一层**就挡下来（零工具 + 交回 planner 重决策；这里只认机器能
            # 保证的形态，不替它判断"整理得好不好"——那一维靠弹卡给人眼看）。
            return [], (f"notice_send 的正文看起来是**占位符**（{ph}）：不调用任何工具，"
                        "重新决策——正文要写主人这轮真正要说的那句话（可以按他的意思"
                        "整理成一句得体的通知，但**不能**拿工具名/技能名/`[这样]`的"
                        "占位符顶上，也不能整个换成一句与主人原话无关的通用语）")
        title = _write_arg(params.get("title"))
        if title and len(title) > _NOTICE_TITLE_LIMIT:
            return [], (f"notice_send 的标题太长（{len(title)} 字，上限 "
                        f"{_NOTICE_TITLE_LIMIT} 字）：不调用任何工具，"
                        "如实向主人说明太长、请他给短一点的标题")
        args = {"name": target, "content": content}
        if title:
            args["title"] = title
        return ([_spec("send_user_notice", args)],
                (f"给后台账号「{target}」发一条站内通知"
                 f"（标题「{title or '站内通知'}」）"
                 "——**收件人的账号名必须能在后台账号列表里看到**；"
                 "正文会印在确认卡上由主人核对**全文**（发出后没有撤回的通道）"))

    if name in ("quota_approve", "quota_reject"):
        # 队列里的那两件（20260929 批 H 起按 **user_id**）：它们治的是"申请表里等着
        # 办的那一行"，编号由系统印在台账帧与 `list_quota_requests` 的行上
        # （`账号 id=<编号>`），模型只负责原样抄。**账号 id 与账号名只在这一支上
        # 分家**——`quota_reset` 仍按名字，那一件不需要他申请过、也就不在任何队列里
        # （见技能定义上的注）。
        #   · 解不出编号 ⇒ 零工具 + 追问（**响亮**，不当"没填"）：工具侧已经没有名字
        #     通道了，拿一个名字去猜 uid 等于把额度清给另一个人。
        # 方向由**技能名**定死而不是参数（同冻结族）：`approved: bool` 那种参数在
        # `graph._confirm_grant_plan` 的"工具 ⊆ 技能 plan"判据下看不见——卡上写
        # 「批准」、实际执行驳回，且无人可见。
        raw = params.get("user_id")
        uid = A.normalize_target_id(raw)
        if uid is None:
            said = _write_arg(raw)
            return [], (f"{name} 缺少申请人的账号 id（user_id，给的是「{said or '空'}」）："
                        "不调用任何工具，如实向主人说明要先把申请队列读一次、"
                        "把申请人那一行的编号原样抄下来"
                        "（台账帧或申请队列的行上印着 `账号 id=<编号>`；"
                        "这一件**不认账号名**）")
        if name == "quota_reject":
            # 驳回理由**必填**，而且与 `notice_send` 的正文同一条纪律：它是**以主人
            # 的名义**发给申请人的那句话（会原样进站内通知）。主人没说就问他要——
            # **绝不替他编一句**（申请人收到的是主人的裁决，理由写错比不写更糟）。
            reason = _write_arg(params.get("reason"))
            if not reason:
                return [], ("quota_reject 缺少驳回理由（reason）：不调用任何工具，"
                            "如实向主人问一句为什么驳回——**不要**替他写一个理由："
                            "那条理由会以他的名义发给申请人")
            if len(reason) > _QUOTA_NOTE_LIMIT:
                return [], (f"quota_reject 的理由太长（{len(reason)} 字，上限 "
                            f"{_QUOTA_NOTE_LIMIT} 字）：不调用任何工具，"
                            "如实向主人说明太长、请他说短一点")
            ph = _notice_placeholder(reason)
            if ph:
                return [], (f"quota_reject 的理由看起来是**占位符**（{ph}）："
                            "不调用任何工具，重新决策——理由要写主人这轮真正给的"
                            "那一句（不能拿技能名/工具名/`[这样]`的占位符顶上）")
            return ([_spec("reject_quota_request", {"user_id": uid, "reason": reason})],
                    (f"驳回账号 id={uid} 的对话额度重置申请（额度不变，"
                     f"但会给他发一条站内通知、理由「{A.clip(reason, 30)}」"
                     "——**发出后没有撤回的通道**；卡面会印出申请人是谁）"))
        return ([_spec("approve_quota_request", {"user_id": uid})],
                (f"批准账号 id={uid} 的对话额度重置申请（他的额度恢复到上限、"
                 "马上能继续提问；**批准不可撤销**；卡面会印出申请人是谁）"))

    if name == "quota_reset":
        # **主动**重置（20260929）：与上面两件不同——它不需要对方申请过，因此目标
        # 不在队列里、没有系统印的编号可抄 ⇒ 仍走**账号名**通道（与冻结/通知族同）。
        #   · 缺名字 ⇒ 明写"不要拿你猜的名字顶上"（名字是唯一通道）；
        #   · 纯数字 ⇒ 后台账号列表里只有名字，没有编号可写（工具**没有** user_id
        #     参数：账号族不列超管那一行，那条防线只在"定位必须经过列表"时成立）。
        target = _write_arg(params.get("name"))
        if not target:
            return [], ("quota_reset 缺少账号名（name）：不调用任何工具，"
                        "如实向主人问清要重置的是哪一个后台账号——**不要**拿你猜的"
                        "名字顶上，也不要用账号编号")
        if target.isdigit():
            return [], (f"quota_reset 给的是一串数字「{target}」：不调用任何工具，"
                        "如实向主人问清那个账号的**名字**（后台账号列表里看得见的那一行；"
                        "这一件不按编号操作账号）")
        return ([_spec("reset_user_quota", {"name": target})],
                (f"**主动**把后台账号「{target}」的对话额度恢复满额"
                 "（**不需要他申请过**的一次重置，他那边不会收到任何提示；"
                 "恢复满额不可撤销）"
                 "——**要动的那个名字必须能在后台账号列表里看到**"))

    return [], f"{name}：未知的写技能（不调用任何工具）"


def _norm_id_or_ref(value):
    """实参 → 正整数 **或** 参数引用字面量（`$list_notes[0].noteId`）；都认不出 → None。

    为什么 id 位置也要认引用（20260923 批 7）：planner 提示词规则 3b 说得很死——
    "只要某个参数的值来自本轮已执行工具的返回，就**一律优先用引用**"。它照办时写
    `{"article_id": "$search_notes[0].noteId"}`，而 `_norm_pos_int` 只认数字 ⇒ 一条
    合法的计划被当成"缺参"退回追问（步骤白跑一轮）。取值由 `agent/refs.py` 在
    execute 调用前完成（解析失败给原因码，走既有 blocker 链路），这一层只负责
    **认得出那是个引用**、原样透传。

    反面教训（20260921 复盘）：写技能把"解不出的引用"当"没填"是**静默降级**——
    能力看似在、实际永远走不到。这里的态度是"认得就放行，认不得就响亮地问"。
    """
    if is_ref(value):
        return str(value).strip()
    return _norm_pos_int(value)


def _norm_id_list(value) -> list[int]:
    """实参 → 正整数 id 列表（去重保序；认不出的项丢掉——**绝不猜编号**）。

    与 `tools.base._as_ids` 同判据（那一层还要再收一遍）：plan 是模型产出的文本，
    `ids` 可能是 `[7, "8"]`、`"7"`、`None`，甚至一句自然语言（丢掉 → 空列表 →
    零工具追问，方向与全表一致）。
    """
    if value is None or isinstance(value, bool):
        return []
    items = value if isinstance(value, (list, tuple, set)) else [value]
    out: list[int] = []
    for it in items:
        n = _norm_pos_int(it)
        if n is not None and n not in out:
            out.append(n)
    return out


def _norm_true(value) -> bool:
    """实参 → 是不是"全部"（只认明确的肯定；**认不出 = 不是**——绝不默认 True）。"""
    if isinstance(value, bool):
        return value
    s = str(value if value is not None else "").strip().lower()
    return s in ("true", "1", "yes", "y", "on", "all", "全部", "全都", "所有")


def _expand_own_workflow(workflow, params: dict) -> tuple[list[str], str]:
    """用户**自己**的数据写技能（20260923 批 7）→ (TOOLS 行清单, 注记)。

    与 `_expand_write_workflow` 分开写而不是塞进去：这几件的差别不在措辞而在**参数
    形状**——收藏两件是"一个 id"，标记已读是"一串 id 或者'全部'"这个**二选一**，
    且二选一里"都没给"必须**零工具**（不能默认 all=true：那是替主人把全部未读
    一次清掉，而这一步**不可逆**）。塞进那个函数只会让"名字通道"那套判据在这里
    变成恒假的噪声。

    注记非空 + tools 为空 = "参数不齐"的零工具路径（planner 据此去追问）；
    两头都非空 = 正常路径的说明。
    """
    name = workflow.name

    def _spec(tool_name: str, args: dict) -> str:
        return f"{tool_name}({json.dumps(args, ensure_ascii=False)})"

    if name in ("favorite_add", "favorite_remove"):
        aid = _norm_id_or_ref(params.get("article_id"))
        if aid is None:
            return [], (f"{name} 缺少文章 id（article_id）：不调用任何工具，"
                        "如实向主人问清是哪一篇文章；若不知道 id，"
                        "先用 search_notes / list_notes 读回确切 id，"
                        "或用参数引用（$<工具名>[<序号>].<字段名>）让系统去取"
                        "（取消收藏还可以先用 list_my_favorites 看他收藏夹里有哪几篇）")
        # 注记里不回显引用语法（plan 文本会进 narrator 的系统提示——规则 3b 明确要求
        # 别把这段语法展示出来；取值由 refs 层完成，叙述层只需要知道"是上一步那篇"）。
        shown = "上一步读到的文章" if is_ref(aid) else f"文章 {aid}"
        if name == "favorite_add":
            return [_spec("add_favorite", {"article_id": aid})], (
                f"把{shown}收进**他自己**的收藏夹（已经收藏过时工具会如实说明，"
                "不会重复收藏；不改文章的公开状态）")
        return [_spec("remove_favorite", {"article_id": aid})], (
            f"把{shown}从**他自己**的收藏夹里去掉（本来就没收藏时工具会如实说明）")

    if name in ("notice_read", "message_read"):
        # 两件同一形状（"一串 id 或者全部"这个二选一，都没给就零工具追问）；
        # 差别只在**物件**与措辞：通知 vs 收到的信（且信只有收到的能标）。
        thing = "通知" if name == "notice_read" else "站内信"
        read_tool = "read_notifications" if name == "notice_read" else "read_messages"
        list_tool = "list_notifications" if name == "notice_read" else "list_my_messages"
        ids = _norm_id_list(params.get("ids"))
        want_all = _norm_true(params.get("all"))
        if want_all and ids:
            return [], (f"{name} 同时给了 all 和具体 id（一个说\"全部都标\"、"
                        f"一个说\"就这几条\"）：不调用任何工具，如实向主人问清要标哪一些")
        if want_all:
            return [_spec(read_tool, {"all": True})], (
                f"把**全部**未读的{thing}标记为已读（不可逆：标的就不再是未读）")
        if ids:
            shown = "、".join(str(i) for i in ids)
            return [_spec(read_tool, {"ids": ids})], (
                f"把{thing} {shown} 标记为已读（不可逆：标的就不再是未读）；"
                f"id 没对上的（不在他收件箱里 / 本来就不存在）会如实说明")
        # 既没给 id 也没说"全部"：**零工具**，问清楚再来（见函数头注：默认全部
        # 等于替主人做一次不可逆的操作）。
        return [], (f"{name} 没有指出要标记哪些{thing}（ids 与 all 都没给）："
                    f"不调用任何工具，如实向主人问清是把**全部**未读标掉、还是只标某几条"
                    f"（若是某几条，先用 {list_tool} 读出它们的 id 再回来）")

    return [], f"{name}：未知的写技能（不调用任何工具）"


def _expand_todo_workflow(workflow, params: dict) -> tuple[list[str], str]:
    """后台首页待办 / 日程（20260926 第八轮）→ (TOOLS 行清单, 注记)。

    与 `_expand_write_workflow` 分开写（同 `_expand_own_workflow` 的理由）：那两套的前提
    都是"目标能在站内核对"——一个按名字解析 id，一个认 article_id / 通知 id。这一件
    的目标是主人随口说的一件事，**站内没有东西可核对**；把它塞进名字通道，只会让
    `_owner_target_span` 那套"目标名必须能从主人原话里抽出来"的判据作用在一段自由
    文本上（那套判据是为了防"模型自己编了一个目标名"，而这里正文本来就该是主人的话
    ——**判据错位比没有判据更糟**：它会把好参数判红）。

    判据只剩两条，都是"缺了就不写"：
      · 正文空 → 零工具 + 注记（"记一下"三个字本身就是正文时，那是主人的口误，
        不是一件待办）；
      · 排期翻不出来 → 零工具 + 注记，**绝不挑一个日子顶上**（翻成什么由
        `A.normalize_due_date` 一处决定；这里翻出来的值同时进 TOOLS 行、确认框与
        令牌载荷，三处是同一个值）。
    超长也在这里挡（**不截断**：截断等于替主人改字，同 Rust 侧的取舍）。
    """
    text = _write_arg(params.get("text"))
    if not text:
        return [], ("dashboard_todo_add 缺少正文（text）：不调用任何工具，"
                    "如实向主人问清要记的是哪一件事；**不要**拿他这句话本身当正文猜一个")
    if len(text) > _TODO_TEXT_LIMIT:
        # 上限与服务端同源（tools/base.py 的 `_TODO_TEXT_LIMIT` = Rust MAX_TEXT_CHARS）；
        # 这里挡一道只是为了"零写 + 能说清原因"，服务端那一道才是判据。
        return [], (f"dashboard_todo_add 的正文太长（{len(text)} 字，上限 {_TODO_TEXT_LIMIT} 字）："
                    "不调用任何工具，如实请主人把这件事说短一点（**不许**替他截断）")
    raw = _write_arg(params.get("date"))
    due = A.normalize_due_date(raw) if raw else None
    if raw and due is None:
        return [], (f"dashboard_todo_add 的排期日「{raw}」认不出来"
                    f"（只认 {A.DUE_DATE_FORMS_CN}）：不调用任何工具，"
                    "如实向主人问清是哪一天——**不许**自己挑一个日子顶上")
    args: dict = {"text": text}
    if due:
        args["date"] = due
    return ([f"create_dashboard_todo({json.dumps(args, ensure_ascii=False)})"],
            (f"往他**自己**后台首页的待办里加一条「{A.clip(text, 20)}」"
             + (f"，排期 {A.due_date_cn(due)}" if due else "（未排期）")
             + "；只追加这一条，列表里原有的都不动"))


def _expand_todo_done_workflow(workflow, params: dict) -> tuple[list[str], str]:
    """待办「勾完成」（20260926 第十轮）→ (TOOLS 行清单, 注记)。

    与 `_expand_todo_workflow`（追加一条）共用"目标是自由文本"这个前提，**但判据更少**：
    那一件还要多一道"排期翻不翻得出来"（它有 date 参数），这一件只认 text。剩下的
    两条都是"缺了就不写"：

      · 正文空 → 零工具 + 注记（"勾一下"三个字里没有可勾的对象，去问主人）；
      · 正文超长 → 零工具 + 注记（**不截断**，同 add 的取舍：截断等于替主人改字，
        而这里改字会让"同一行"变成"另一行"）。

    **计划层不判"这一条在不在列表里"**：那份列表在这一层读不到（它要经弹卡那一轮
    惰性读一次，见 `graph._confirm_popup`），而把"查无此条 ⇒ 零工具"写在这里会把
    "读不到列表"与"列表里没有这一条"混成同一件事——前者是系统读不到，后者是主人
    说错了正文。真正的定位判据在工具侧对着**实时**列表做一次，那一次同时是写前读。
    """
    text = _write_arg(params.get("text"))
    if not text:
        return [], ("dashboard_todo_done 缺少正文（text）：不调用任何工具，"
                    "如实向主人问清指的是哪一条（可先选 dashboard_todo_list 把列表读出来"
                    "给他挑，**不要**替他挑一条）")
    if len(text) > _TODO_TEXT_LIMIT:
        return [], (f"dashboard_todo_done 的正文太长（{len(text)} 字，上限 {_TODO_TEXT_LIMIT} 字）："
                    "不调用任何工具，如实请主人照列表里的写法说短一点（**不许**替他截断）")
    return ([f"complete_dashboard_todo({json.dumps({'text': text}, ensure_ascii=False)})"],
            (f"把他自己后台首页的待办里「{A.clip(text, 20)}」那一条勾成完成"
             f"（只翻完成标记，正文与排期都不动）"))


def _expand_todo_reschedule_workflow(workflow, params: dict) -> tuple[list[str], str]:
    """改某一条待办的排期日（20260929 批 G）→ (TOOLS 行清单, 注记)。

    与 `_expand_todo_done_workflow` 同一族（目标是一段自由文本，站内没有东西可核对，
    连"这一条在不在列表里"都要到工具侧对着实时列表判），差别只有一处：**日期是
    这一件事的全部内容**，所以它必须说清楚。而"说清楚"要分**三**种处境，别混：
      · 契约词「未排期」⇒ **清空**（主人说"不用排期了/把日子清掉"就是它，见
        `adminops._TODO_CLEAR_WORD`；这是与"没说日子"完全不同的第三态）；
      · 说了日子、但翻不出来 ⇒ 零写 + 追问（**绝不挑一天顶上**——错一天的日程会
        静静地躺在后台日历的错误格子里）；
      · **压根没说**日子 ⇒ 零写 + 追问，问的是另一句（"要改到哪一天"）。
    三种出口的注记各不相同：planner 同轮纠偏读的就是这段正文（`graph.param_correct`）。
    """
    text = _write_arg(params.get("text"))
    if not text:
        return [], ("dashboard_todo_reschedule 缺少正文（text）：不调用任何工具，"
                    "如实向主人问清改的是哪一条（可先选 dashboard_todo_list 把列表读出来"
                    "给他挑，**不要**替他挑一条）")
    if len(text) > _TODO_TEXT_LIMIT:
        return [], (f"dashboard_todo_reschedule 的正文太长（{len(text)} 字，上限 "
                    f"{_TODO_TEXT_LIMIT} 字）：不调用任何工具，如实请主人照列表里的写法"
                    f"说短一点（**不许**替他截断）")
    raw = _write_arg(params.get("date"))
    if not raw:
        return [], ("dashboard_todo_reschedule 没给日期（date）：不调用任何工具，"
                    f"如实向主人问清要改到哪一天（他要是说不要排期了，date 就填"
                    f"「{A._TODO_CLEAR_WORD}」）")
    if raw in A._TODO_CLEAR_DATES:
        # 清空：传**契约词**（不是主人嘴里那个说法）——工具与卡面都只认这一个字面。
        return ([f"reschedule_dashboard_todo("
                 f"{json.dumps({'text': text, 'date': A._TODO_CLEAR_WORD}, ensure_ascii=False)})"],
                f"把他自己后台首页的待办里「{A.clip(text, 20)}」那一条的**排期清掉**"
                f"（只动排期，正文与完成标记都不动）")
    due = A.normalize_due_date(raw)
    if due is None:
        return [], (f"dashboard_todo_reschedule 的排期日「{raw}」认不出来"
                    f"（只认 {A.DUE_DATE_FORMS_CN}；清空排期填"
                    f"「{A._TODO_CLEAR_WORD}」）：不调用任何工具，"
                    "如实向主人问清是哪一天——**不许**自己挑一个日子顶上")
    return ([f"reschedule_dashboard_todo("
             f"{json.dumps({'text': text, 'date': due}, ensure_ascii=False)})"],
            (f"把他自己后台首页的待办里「{A.clip(text, 20)}」那一条的排期改成"
             f"{A.due_date_cn(due)}（只动排期，正文与完成标记都不动）"))



# 计划状态闭集（`PLAN_STATUS_*`）已下沉到 `agent/plan_status.py`（20261017 批 2 刀 2c）：
# 消费方全在别处（`plan_codec` / `graph` / `decisions` / `eval`），本模块顶部 import 回来
# （迁移期转发，收尾刀整块消失）。


# 参数名别名层（`_PARAM_ALIASES` / `_param_alias_fix`）已下沉到
# `agent/workflow_params.py`（20261017 批 2 刀 2c）：它与参数 schema 同住一层（读
# `workflow_param_specs`），本模块顶部 import 回来（迁移期转发）。


def _expand_article_status(params: dict,
                           aids: list[int]) -> tuple[list[str], str, str | None]:
    """文章类写技能的**状态**那一半：把点名的 N 篇改成点名的 status / is_top。

    （20261015 批 1 从 `_instantiate_plan` 抽出来：文章通道那两支合起来让那个函数顶到
    行数棘轮，而这一支自身就是一件完整的事——C=13 / 32 行，两项都在预算内。）
    ⚠️ 另一半 `article_tags` **没有**一起抽：它单独成函数是 93 行 / C=32，当场撞上本仓
    `C ≤ 30` 的预算（`tests/test_complexity_budget.py`）⇒ 要么再拆两半、要么别动；它与
    `graph._drop_satisfied_tag_creates` 的摘除判据紧耦合（见那段长注），拆它得把那一对
    同源关系一起重读，留到那一族下次改动时一起做。

    归一（status / is_top）在**这一层**做，归不出来 ⇒ 零工具 + 问清：写操作最不该做的事
    就是"参数看不懂时猜一个"（猜出来的那一版会当真写进去）。

    ⚠️⚠️ **第三个返回值是既有行为里的一处同名捕获，本批原样保留**：从前这一支住在一个
    也叫 `status` 的局部变量**同名**的 `_instantiate_plan` 里（那个 `status` 是导航族
    用来记 `nav_iot_off` / 最终写进计划 `status` 字段的那个字符串）⇒ 这一支的归一结果
    **会盖掉它**：`instantiate_plan("article_status", {"article_id": 46, "status":
    "private"})` 的计划 `status` 字段是 **`"private"`** 而不是 `"executed"`（两臂实测：
    611 格里恰好这 3 格不同）。它同时**关掉**了下面那句"零工具轮不许不成账"（那个判据
    读的也是 `status`）。**看起来像缺陷**（计划 status 字段的语义应该是执行状态，不是
    文章的目标状态），但改它是**行为变更**、要单独一趟验证 ⇒ 这里按原样把值回传出去，
    不在"零行为变更"这一批里顺手改掉；换名 = 那 3 格当场变成 `executed`，谁要动它请连
    这条注一起改并跑 golden。
    ⇒ 因此签名第三项**如实**写 `str | None`（旧代码里 `status` 被重新绑定成 `str | None`
    也是同一个 mypy 错误）；照原样回传的代价是**调用点那一句留住了一个既有的计数错误**
    （本仓没有一处 `# type: ignore`，220 个错误就是这么记着账的）——别拿 ignore 把它抹掉。
    """
    status = A.normalize_status(params.get("status"))
    top = A.normalize_top(params.get("is_top"))
    # ⚠️ 注记**先落到变量上再回传**，别直接写成 `return [], "中文…", status`：`eval/wordlist_gate.py`
    # 的第二层按 AST 数"住在集合字面量里的中文串"，元组的直接元素也算一处 ⇒ 同一个字面量
    # 换个位置就会被记成"新增了一张中文表"（实测：这一支原本就是这样，红了一次）。
    if params.get("status") not in (None, "") and status is None:
        note = (f"status「{params.get('status')}」认不出来（只支持 "
                "public/private/draft）：不调用任何工具，如实向主人问清")
        return [], note, status
    if params.get("is_top") not in (None, "") and top is None:
        note = (f"is_top「{params.get('is_top')}」认不出来（只支持 1/0）："
                "不调用任何工具，如实向主人问清")
        return [], note, status
    if status is None and top is None:
        note = ("article_status 没有指出要改什么（status / is_top）："
                "不调用任何工具，如实向主人问清要改成什么")
        return [], note, status
    # **一篇一条 spec**（20261009，与 `tag_create.titles` 同一条纪律）：N 篇 ⇒ N 条
    # `set_article_status`，多 spec 的现成通道全部自动接上（卡印成清单 + 「全部办」、
    # 逐条执行、逐条回执、逐条"已是目标值"摘除）。N=1 时逐字节等于旧行为（键序
    # article_id→status→is_top 不变）。
    # `dict[str, object]`：两个值的类型不同（归一后的 status 是 `str`、is_top 是 `int`），
    # 而它们要一起进 `json.dumps` ⇒ 按值域写 object（旧代码写 `fields = {}`，mypy 按
    # 第一笔赋值推成 `dict[str, str]`，于是 `fields["is_top"] = top` 一直是条计数错误）。
    fields: dict[str, object] = {}
    if status is not None:
        fields["status"] = status
    if top is not None:
        fields["is_top"] = top
    out = [f"set_article_status({json.dumps({'article_id': one, **fields}, ensure_ascii=False)})"
           for one in aids]
    note = (f"修改文章 {'、'.join(str(x) for x in aids)}："
            + "、".join(filter(None, [
                f"状态→{A.status_cn(status)}" if status is not None else "",
                f"置顶→{A.top_cn(top)}" if top is not None else ""]))
            + "（只改点名的字段）")
    return out, note, status


def instantiate_plan(workflow_name: str, params: dict,
                     role: str | None = None) -> dict:
    """公开入口：`_instantiate_plan` 的产物 + **统一补齐 `param_unknown`**。

    **为什么要有这层薄壳**（20260926 批 5）：`param_unknown`（"planner 写了、没人读"的
    参数名）此前**只有两个分支填**——navigate 与通用模板，因为只有那两条路调用了
    `check_workflow_params`。写技能（`_expand_*`）、content_query、read_article 三条路
    **从不填** ⇒ planner 在那些技能里写的臆造参数名**完全无声**：既不记账也不纠偏，
    工具照常执行、planner 以为自己填的东西被读了。全量 951 份 trace 实测：1157 次决策里
    有 **69 处**"没人读的参数名"，其中 **68 处是静默的**（唯一的例外是一次 chat 的
    `reply`，它恰好走通用分支）——按技能分：`navigate.mode` 58（该校验 20260925 才接上）、
    `tag_create.parent_id` 8（**写技能分支从未接线**，正是 20260921 那次事故的现场：
    参数被静默忽略、标签建到了错的层级）、`chat.reply/response` 3。

    做成壳而不是"每个分支记得填"：**唯一入口谁都漏不掉**（同本仓"枚举只有一个入口"
    `iter_trace_files` 的纪律）。语义与既有完全相同——`[n for n in params if n not in
    specs]` 就是 `check_workflow_params` 的 `unknown`，所以已经填过的那两条分支取值不变。
    **只记账不阻断**（既有语义：工具照常执行，planner_node 打 WARNING + trace 事件）。
    """
    # 参数名归一（20261005）**必须早于 `_instantiate_plan`**：写族展开、
    # `check_workflow_params` 都在那里面读 `params.get("title")`，晚一步搬就等于没搬
    # （`title` 照旧 missing、零工具照旧发生）。搬完再算 `param_unknown`——被搬走的
    # 那个名字已经在正确的槽上，不该再报"没人读"。
    fixed, moved = (_param_alias_fix(WORKFLOW_MAP.get(workflow_name) or WORKFLOW_MAP["chat"], params)
                    if isinstance(params, dict) else (params, []))
    plan = _instantiate_plan(workflow_name, fixed, role)
    if isinstance(fixed, dict) and isinstance(plan, dict):
        sk = WORKFLOW_MAP.get(plan.get("workflow"))
        if sk is not None:
            specs = workflow_param_specs(sk)     # 与两个分支读的是同一份规格
            plan["param_unknown"] = [n for n in fixed if n not in specs]
        if moved:
            plan["param_alias"] = [{"src": s, "dst": d} for s, d in moved]
    return plan


def _instantiate_plan(workflow_name: str, params: dict,
                      role: str | None = None) -> dict:
    """技能模板 + 参数 → 结构化计划。

    返回 {"workflow", "tools"(list[str]), "note"(str), "reply"(str), "chat"(bool),
    "status"(str)}。planner_node 据此编码 plan 字段文本。`status` 是本函数的
    **确定性产出**（见 `PLAN_STATUS_VALUES` 那段）——planner LLM 一个字都不填，
    它吐的是自由文本、没有这个字段，指望它自评就等于把判据交回给模型。
    `param_unknown` 由**外面的公开入口**统一补齐（见 `instantiate_plan` 的注），
    本函数里的那几个分支是历史现场——**别照着它们逐分支补**，也别删那层壳。
    特殊处理：
      - navigate：target 经 NAV_MAP 映射；映射为 None（已下线）→ 不调用工具、如实告知；
        未识别别名 → 如实告知没有该页面；confirm **恒 False**（20260926 删掉 mode 参数：
        跳转一律直达，确认卡已停用）

    `role`（20260924）= 本轮调用者角色，只影响 content_query 的 `PARAMS.calls`
    白名单（见 `callable_query_tools`：管理员多一份后台只读清单）。**默认 None =
    公开清单**——`planner_node` 必须把本轮 role 传进来；漏传的后果是**静默的**：
    管理员点名的后台读工具在 calls 里被剔除 ⇒ 剔空纠偏 ⇒ planner 反复重规划
    （`admin_notes_console_list` 实测每次跑满 4 轮就是这条路径）。其余调用点
    （`_*_fix` 校正器、decisions 快道）重建的都是自己构造的参数、且技能都不是
    content_query，传 None 无害；但凡能把 planner 产出的计划整体重建一遍的地方，
    都应当把 role 一并带上。
    """
    workflow = WORKFLOW_MAP.get(workflow_name) or WORKFLOW_MAP["chat"]
    tools: list[str] = []
    dropped: list[str] = []  # 白名单剔除的 planner 点名项（planner_node 记账，见下）
    param_unknown: list[str] = []  # PARAMS 里没人读的参数名（只读分支填，见通用分支）
    # 本分支有没有**消费** PARAMS.tools / PARAMS.calls（20260925 批 C）。只有
    # content_query 分支读这两键；其余技能读不到却也不记账 ⇒ 清单凭空消失。见下方
    # 收尾处那段 `_workflow_no_calls_suffix` 的注。
    consumed_calls = False
    note = ""
    # 计划状态（`PLAN_STATUS_VALUES`）：下面每个"系统已知的处境"出口各写一个值；
    # 收尾处再按 tools/技能补默认（executed / answer_only）。**空串 = 不知道**，
    # 消费侧一律 fail-open（宁可漏判也不误伤）——但空串只应该出现在手写的夹具文本
    # 或旧版计划上，系统性计划一定有值（`plan_encode` 写那一行）。
    status = ""
    if workflow.name == "navigate":
        # 跳转恒直达（20260926）：下面三条出口（映射命中/字面路径/模糊归一）都写
        # `confirm: False`。原来的 `mode` 参数（direct/suggest）已删——见技能定义里
        # 那段「推荐某个页面不该变成跳转」，`navigate_to` 的 confirm 形参保留未动。
        # target 的事前校验（20260925）：**必须排在本技能自己的映射表判据之前**。
        # 现状是 target 为空时一路落到最后的"无法识别导航目标「」"——那会给访客一句
        # **假话**（"站内没有该页面"），而真相是参数没给（`required_params=("target",)`
        # 就是为此声明的）。`path`/`confirm` 不在 `inputs` 里 ⇒ 不在参数表里，planner
        # 真填了会被 `unknown` 记账（它们由本分支从 NAV_MAP 算出，是模板里的死占位符）。
        specs = workflow_param_specs(workflow)
        chk = check_workflow_params(workflow, params, specs)
        param_unknown = chk["unknown"]
        if chk["missing"] or chk["bad"]:
            return _param_problem_plan(workflow, chk, specs)
        target = (params.get("target") or "").strip()
        mapped = NAV_MAP.get(target)
        if target in NAV_MAP and mapped is None:
            # 映射表标记为不可导航，**两种处境的话术不同**（判据是同一条：
            # 这个别名归 `_IOT_NAV_ALIASES` 还是别的）：
            #   · IoT 关掉：本站从来没装这个平台 ⇒ "未部署"
            #   · 友链等：曾经有过、后来撤了 ⇒ "已下线"
            # 说反了的代价是具体的：把"没装"说成"已下线"，访客会去问站主为什么撤掉。
            if target in _IOT_NAV_ALIASES:
                note = _IOT_OFF_NOTE
                status = "nav_iot_off"
            else:
                note = f"导航目标「{target}」已下线：如实告知访客，不调用任何工具"
                status = "nav_offline"
        elif mapped:
            args = {"path": mapped, "confirm": False}
            tools.append(f"navigate_to({json.dumps(args, ensure_ascii=False)})")
            note = f"目标页: {target} → {mapped}"
        elif target.startswith("/"):
            # 字面路径：预校验白名单（单一事实来源 = 工具层常量）。白名单外的路径
            # 直接给"不存在"注记、零工具——不让模型拿着无效路径自行发挥（行为不稳，
            # 可能替身跳真实页/出确认帧）；白名单内直用路径。语义推断同样是禁止项
            # （把 /iot 猜成 /device-console/ 属于替身导航）。
            if nav_path_valid(target):
                args = {"path": target, "confirm": False}
                tools.append(f"navigate_to({json.dumps(args, ensure_ascii=False)})")
                note = f"目标页: {target}（字面路径，白名单校验通过）"
            elif not IOT_ENABLED and target.rstrip("/") == IOT_NAV_PATH.rstrip("/"):
                # 字面写出 IoT 路径而平台没装：白名单已经收掉了它，落到下面那支的话
                # 话术会变成"站内不存在这个页面"——不算错，但这不是"不存在"，是"没装"。
                # 判据仍是开关那一处，不是路径白名单（白名单是它的结果，不是它的源）。
                note = _IOT_OFF_NOTE
                status = "nav_iot_off"
            else:
                note = (
                    f"导航目标「{target}」不存在：如实告知没有该页面，不调用任何工具，"
                    f"可参照真实页面（{_NAV_REF_HINT}）给出建议（文本链接即可）"
                )
                status = "target_unreachable"
        else:
            # 不在映射表：先试口语模糊归一（关键词规则，确定性），
            # 命中即等同映射命中；仍不命中才"无法识别、如实告知"
            hit, fuzzy_hit = fuzzy_nav_hit(target)
            if hit and fuzzy_hit:
                args = {"path": fuzzy_hit, "confirm": False}
                tools.append(f"navigate_to({json.dumps(args, ensure_ascii=False)})")
                note = f"目标页: {target}（口语模糊归一）→ {fuzzy_hit}"
            elif hit:
                # 命中了规则、但那个页面本站没有（当前只有 IoT 这一种）：
                note = _IOT_OFF_NOTE
                status = "nav_iot_off"
            else:
                note = (
                    f"无法识别导航目标「{target}」：如实告知没有该页面，不调用任何工具，"
                    f"可参照真实页面（{_NAV_REF_HINT}）给出建议（文本链接即可）"
                )
                status = "nav_unresolved"
    elif workflow.name == "read_article":
        # 系统快道专用：article_id 由 planner_node 从 current_url 解析注入。
        # 缺失时按 chat 兜底（绝不生成 article_id=null 的非法工具调用——若
        # 未来 planner LLM 误选本技能，这就是最后防线）。
        aid = params.get("article_id")
        if aid is None or str(aid).strip() == "":
            tools = []
            note = "read_article 缺少 article_id（当前页面非文章详情页？），按闲聊处理"
        else:
            args = {"article_id": int(aid) if str(aid).isdigit() else aid}
            tools.append(f"get_article_detail({json.dumps(args, ensure_ascii=False)})")
            note = f"读取当前文章全文（ID={aid}）"
    elif workflow.name == "content_query" and (params.get("tools") or params.get("calls")):
        consumed_calls = True
        # 20260903 架构裁决（planner 全权）：内容查询的调用清单由 planner 产出——
        # params.tools（无参只读点名，白名单 explicit_tools(role)）或 params.calls
        # （带参检索调用，白名单 callable_query_tools(role)）。两条白名单都**按
        # 本轮角色**取（20260924：管理员多一份后台只读项，见 callable_query_tools
        # 与 explicit_tools 的注）。两层白名单校验，非法/
        # 重复条目剔除（合法条目仍生效——不因模型多写一个越权工具就整单作废）；
        # 调用清单为空 = planner 决策无需工具（收尾轮）——不再是"自由 ReAct"。
        # 20260913：剔除项记入 dropped 返回给 planner_node（WARNING + trace 事件）
        # ——此前静默丢弃，planner 以为计划已执行、narrator 照计划声称"我调用了 X"，
        # 而 agent.log 里毫无痕迹（15:51 trace 实证）。
        picked: list[str] = []
        explicit = params.get("tools")
        if isinstance(explicit, list):
            # 无参点名通道**也按角色取**（20260924 第二版）：菜单里那 5 个后台读
            # 工具是无参的，planner 自然写进 PARAMS.tools——只看 calls 会让这条
            # 通道继续剔空（golden trace 20260924T055910 实证：仍跑满 4 轮）。
            allowed_explicit = set(explicit_tools(role))
            for t in explicit:
                if not isinstance(t, str):
                    dropped.append(str(t))
                elif t.strip() not in allowed_explicit:
                    dropped.append(t.strip())
                elif t.strip() not in picked:
                    picked.append(t.strip())
        calls = params.get("calls")
        if isinstance(calls, list):
            # 白名单按本轮角色取（20260924）：管理员多一份后台只读清单。取一次算好，
            # 别在循环里重复构造（这条路径每轮规划都走）。
            allowed = callable_query_tools(role)
            for c in calls:
                if not isinstance(c, dict) or not isinstance(c.get("tool"), str):
                    dropped.append(str(c))
                    continue
                cname = c["tool"].strip()
                if cname not in allowed:
                    dropped.append(cname)
                elif not isinstance(c.get("args"), dict):
                    dropped.append(f"{cname}{DROP_SUFFIX_NOT_OBJECT}")
                else:
                    # 参数**事前校验**（20260925）：同一条通道的"点名了但参数不对"
                    # 此前要等工具侧报错才发现。缺必填/值归不了 ⇒ 这条例目整体剔除
                    # （拼进 dropped，后缀写明原因——`_drop_correction` 认后缀选话术，
                    # 没有后缀它会把参数问题讲成"你够不到这个工具"）；多写的参数名
                    # **只剔掉那个参数**，调用照常（pydantic 本来就忽略多余字段，
                    # 这里只是让"已被忽略"留痕，不再静默）。
                    vchk = check_call_args(cname, c["args"])
                    if vchk["bad"]:
                        dropped.append(f"{cname}{DROP_SUFFIX_BAD_ARGS}"
                                       + "、".join(vchk["bad"]) + "）")
                    else:
                        spec = f"{cname}({json.dumps(vchk['args'], ensure_ascii=False)})"
                        if spec not in picked:
                            picked.append(spec)
                        if vchk["unknown"]:
                            logger.warning("[workflows] %s 的调用带了没人读的参数 %s（已忽略）",
                                           cname, "、".join(vchk["unknown"]))
        # **跨通道折叠**（20261001）：模型会把同一件读**两条通道各写一遍**——
        # 实测（trace `20261001T105116`）`tools: [list_quota_requests,
        # get_moderation_status]` 与 `calls: [{list_quota_requests, {status:pending}},
        # {get_moderation_status, {status:pending}}]` 同时出现 ⇒ 2 件读展开成 4 条调用
        # （主人看到的是"最后一次调用了大量工具，莫名其妙"，而那两条无参读的返回还
        # 整段盖住了带参那条）。无参点名被同一工具的带参调用**覆盖**：带参那次就是
        # 同一件读收窄了条件，无参结果是它的超集，两条都发纯属重复。
        # 折叠只在**工具名粒度**上做——参数不同的两条 calls（如两篇文章详情、
        # 两个关键词）一条都不动，那是各自独立的读。
        # 折叠**不写进 `dropped`**：那不是"够不到/参数错"的剔除，不该触发纠偏重决策
        # （`_drop_correction` 认后缀选话术，这里没有话术可给）；也不会把 `picked`
        # 剔空——`called` 非空就意味着至少有一条带参调用留下。
        called = {t.split("(", 1)[0].strip() for t in picked if "(" in t}
        if called:
            folded = [t for t in picked if "(" not in t and t in called]
            if folded:
                picked = [t for t in picked if "(" in t or t not in called]
                logger.warning("[workflows] 无参点名 %s 已被同一工具的带参调用覆盖（剔除）",
                               "、".join(folded))
        for t in picked:
            if "(" in t:
                tools.append(t)
            else:
                tools.append(f"{t}({{}})")
        if tools:
            note = f"按 planner 决策执行：{'、'.join(tools)}"
    elif workflow.name in WRITE_WORKFLOW_NAMES:
        # 模板占位符当值（20260928）：**写分支的最前面**，先于下面所有按技能名的
        # 分派——因为它判的不是"这个参数填得对不对"，而是"这个值压根不是主人给的，
        # 是模型把技能模板抄了回来"。放在这里才覆盖得住写族的每一个展开函数
        # （名字通道 / 自己数据 / 自由文本 / 文章两件），一处判、全族生效。
        # 用 `_param_problem_plan` 那套现成形状（参数不齐 / 值用不了是**系统已知的处境**，
        # 洞④ 的豁免读的是 status 而不是注记文案）：planner 同轮内重决策，问清或换技能。
        _ph = _unfilled_placeholder(params)
        if _ph:
            return _param_problem_plan(workflow, {
                "fixed": [], "unknown": [], "missing": [],
                "bad": [f"{_ph[0]}={_ph[1]!r}（还是技能模板里的占位符，没有被主人"
                        "原话里的真实值替换）"]})
        # 管理助手写技能（20260921 第二轮起）：**缺参守卫 + 空参剔除**，不复用下方
        # 通用分支。通用分支对缺失参数会实例化出 `{"article_id": null}` 这样的
        # 非法实参（那一路进 JSON 就变成 null，工具侧还得再拦一遍），而写操作最
        # 不该做的事就是"参数不全时猜一个"——
        #   · 目标不明（没有 article_id / 没有标签名）→ **零工具** + 注记，让
        #     planner 去追问，而不是拿 null 去撞 URL；
        #   · 没点名的可选参数一律**不落进 args**（article_status 只发用户点名的
        #     那一项，绝不把 is_top=null 也塞进去——写操作的参数表就是它的语义）；
        #   · 归一（状态/置顶/层级/颜色）都在**这一层**做（adminops 的纯函数），
        #     归不出来就零工具交回 planner；工具侧还有第二道同样的判据（纵深，不互替）。
        #   · 标签 / 分类类写技能全部走 `_expand_write_workflow`（目标按名字，见其头注）；
        #     文章类两件留在下面（它们的目标是 article_id，另有"点名即据"的判据）。
        note = ""
        # 靶通道从**一处声明**读（20261015 批 1）：`_WRITE_TARGET_CHANNEL` 是"这个技能的
        # 靶出自哪个通道"的单一事实源，上面四个桶与这条分派链都从它派生——**从前这里是
        # 同一份分类的第二份手抄**（四个成员判定 + 两个硬写在 fail-closed 排除项里的
        # 技能名），漏一处的后果是静默的（见那张表的头注）。
        _chan = _WRITE_TARGET_CHANNEL.get(workflow.name)
        if _chan in ("name", "ledger"):
            # 两桶（名字/正文片段 ⇄ 台账编号）**共用** `_expand_write_workflow`（缺参
            # 零工具 / 只落点名的参数 / 归一在确定性层做，这套纪律两族一致），差别只
            # 在展开函数里那几支怎么取目标。
            # ⚠️ **展开函数还要按技能名二分**（20260929 批 F）：`review_inbox` 与
            # board_audit 同属台账那一桶，但它的 `params` 是**逐条调用清单**
            # （`_expand_change_set`）。与下面 `_FREE_TEXT_WRITE_WORKFLOWS` 那处二分同一个
            # 坑：落进默认支**不会报错**——`_expand_write_workflow` 会把 `calls` 当成一个
            # 它不认识的参数、静悄悄地零工具零写，主人只看到"什么都没办"。
            if workflow.name == "review_inbox":
                wtools, note = _expand_change_set(workflow, params)
            else:
                wtools, note = _expand_write_workflow(workflow, params)
            tools.extend(wtools)
        elif _chan == "own":
            # 用户自己的数据（20260923 批 7）：收藏两件（article_id）+ 标记已读
            # （ids 或 all，二选一，都没给就零工具追问）。
            wtools, note = _expand_own_workflow(workflow, params)
            tools.extend(wtools)
        elif _chan == "free_text":
            # 目标是**自由文本**的写技能：待办 / 日程（20260926 第八轮 + 第十轮，
            # 20260929 批 G 加第三件）。前两组的目标都能在站内核对（名字 / id），
            # 这一组只有"正文在不在、日期说清楚没有"那几条判据——见三个展开函数的头注。
            # ⚠️ **必须按技能名三分**：桶成员资格只说"目标是自由文本"，而"加一条 /
            # 勾一条 / 改一条的排期"是三个展开函数。默认支（`_expand_todo_workflow`）
            # 对 text/date 都认，所以漏了分支**不会报错**——它会把"勾完成"展开成
            # `create_dashboard_todo`（**多记一条待办**），把"改排期"展开成
            # `create_dashboard_todo({'text':…, 'date':…})`（**又记一条**，而主人以为
            # 只是改了日子）。两次都是"主人收到成功回执、列表却多了东西"。
            if workflow.name == "dashboard_todo_done":
                wtools, note = _expand_todo_done_workflow(workflow, params)
            elif workflow.name == "dashboard_todo_reschedule":
                wtools, note = _expand_todo_reschedule_workflow(workflow, params)
            else:
                wtools, note = _expand_todo_workflow(workflow, params)
            tools.extend(wtools)
        elif _chan != "article":
            # fail-closed（20260923；20261015 批 1 起判据换成"**没有声明靶通道**"）：
            # 落到这里的只可能是"加了新写技能、但没在 `_WRITE_TARGET_CHANNEL` 里给它
            # 声明通道"。写名单是**派生**的（按 plan 的工具作用域），所以这样一件技能
            # 一定进得来、一定落到这里——这正是派生要换来的东西：从前它压根不进写分支，
            # 静默落进下面的通用模板分支。响亮地说出来，并保持零工具零写。
            return {"workflow": workflow.name, "tools": [], "dropped": [], "param_unknown": [],
                    "note": (f"{workflow.name}：新加的写技能没有接入参数展开（系统内部"
                             "配置缺项）：不调用任何工具，如实告知这次没能执行"),
                    "reply": workflow.reply_contract, "chat": False,
                    # "办不成"是系统已知的处境（配置缺项，不是模型判断）
                    "status": "refused"}
        else:
            aid = _norm_pos_int(params.get("article_id"))
            # 多篇形态（20261009）：`article_ids` 数组 + 同一份 status/is_top。两个槽
            # **可以同时给**（模型把第一篇写进单个槽、其余写进数组是常见形态）⇒ 合并成
            # 一份去重保序的清单；只给单个槽时逐字节等价于旧行为。数组本身的语义是
            # "这个动作对这几篇各来一次"（一篇一条 spec），不是"一篇文章几个 id"。
            aids, bad_ids = _pos_int_list(params.get("article_ids"))
            if aid is not None:
                aids = [aid] + [x for x in aids if x != aid]
            if bad_ids:
                # **认不出 ≠ 没给**（见 `_pos_int_list`）：宁可整轮零工具问清，也不许
                # 悄悄只办认出来的那几篇——主人点的是 5 篇、办 1 篇而一个字都不提，
                # 正是 20261009 起写在这里的那条纪律（20261015 补上"值在、但解析不出"
                # 这一格：那一轮 `article_ids` 填成了文本清单，其余三篇无声消失）。
                note = (f"{workflow.name} 的 article_ids 里有认不出的片段"
                        f"（{'、'.join(bad_ids[:3])}）：不调用任何工具，"
                        "如实向主人问清这次要改哪几篇")
            elif not aids:
                note = (f"{workflow.name} 缺少文章 id（article_id）：不调用任何工具，"
                        "如实向主人问清是哪一篇文章；若不知道 id，"
                        "先选 admin_notes 技能读出后台文章清单再回来")
            elif workflow.name == "article_status":
                # 整支抽成函数（20261015 批 1，见其头注）：它自身是独立的一件事。
                # ⚠️ `status` 是**回传的同名捕获**（既有行为、不是这里新引入的）——见那头注
                # 里那两段 ⚠️：直接删掉这个赋值会让 3 格计划 status 当场变样。
                atools, note, status = _expand_article_status(params, aids)
                tools.extend(atools)
            else:  # article_tags
                aid = aid if aid is not None else (aids[0] if len(aids) == 1 else None)
                add, rm, rep = params.get("add"), params.get("remove"), params.get("replace")
                if len(aids) > 1:
                    # `article_ids` 只对 `article_status` 声明（本文具一件只改一篇的标签）。
                    # 模型越界填了多篇 ⇒ **零工具 + 问清**：悄悄只改第一篇、其余不声不响，
                    # 正是本批要治的那个病。
                    note = (f"article_tags 一次只改**一篇**文章的标签（收到了 "
                            f"{len(aids)} 个 id）：不调用任何工具，如实问主人这次先改哪一篇")
                elif rep is not None and (add or rm):
                    note = ("article_tags 的 replace 与 add/remove 同时出现（一个说\"整体替换\"、"
                            "一个说\"增减\"）：不调用任何工具，如实向主人问清意图")
                elif not add and not rm and rep is None:
                    note = ("article_tags 没有指出要加/去/替换哪些标签：不调用任何工具，"
                            "如实向主人问清")
                else:
                    args = {"article_id": aid}
                    for key, val in (("add", add), ("remove", rm), ("replace", rep)):
                        if val is None:
                            continue
                        items = [str(x).strip() for x in val] if isinstance(val, list) else [str(val).strip()]
                        items = [x for x in items if x]
                        if items or key == "replace":
                            # replace=[] 是有语义的（清空标签），必须原样传下去；
                            # add/remove 的空列表没有语义，剔掉。
                            args[key] = items
                    color_spec = _write_arg(params.get("color"))
                    hexval = A.match_tag_color(color_spec) if color_spec else None
                    if not any(k in args for k in ("add", "remove", "replace")):
                        note = ("article_tags 的标签列表都是空的：不调用任何工具，"
                                "如实向主人问清要改哪些标签")
                    elif color_spec and hexval is None:
                        note = (f"color「{color_spec}」不在站内色板里（可选：{A.TAG_COLOR_SPEC}）："
                                "不调用任何工具，如实向主人说明只有这几种颜色，请他挑一个")
                    else:
                        # ── 要加的标签站内还没有 ⇒ **同一张卡里先建再挂**（20261009）──
                        # 现场（trace `20261009T070914`，主人原话「像指明给什么挂标签，就
                        # 一步走完建标签挂标签，意图这么明显还要问第二次拆开浪费成本就算了
                        # 体验太差了」）：主人说「给文章 46 加上 embeding 标签」，planner 按
                        # `article_tags` 的老契约（"不会自动新建，要新建先选 tag_create"）先
                        # 建标签，挂标签那一步只能落到**下一轮**；而下一轮主人只回一个「要」，
                        # 跨轮的挂标签请求在出处闸那里变成零工具收尾——一句话的意图被拆成
                        # 两次弹卡、两次点头，第二次还落空。
                        #
                        # 判据与 `tag_create` 的"一名一 spec"**同源**：一个待建的名字一条
                        # `create_tag`，排在 `set_article_tags` **之前**（execute 按序字面
                        # 执行，先建后挂）。
                        # ⚠️ 这个展开**不知道字典**（`instantiate_plan` 是纯函数）⇒ "这个
                        # 名字站内到底有没有"由 `graph._drop_satisfied_tag_creates` 在
                        # 规划轮收尾前问一次字典、把已有的那些**摘掉**（判据是**任何一层**
                        # 有这个名字就算有）。**不能**只靠 `adminops.reached_specs` 兜：
                        # 它只在**弹卡那一支**跑，而且 create_tag 支的判据是"同名**同层**"
                        # ⇒ 命令式措辞走免弹窗快道时一次都不筛，而"名字在另一层"恰好是
                        # `create_tag` 会**直接新建**（不复用）的那一格 = 库里多一条重名
                        # 一级标签。两处判据的宽窄不同是刻意的，见那两个函数各自的注。
                        # 覆盖 `add` 与 `replace`（20261009 扩面）：两格都是"主人点名了
                        # 这些标签名"，区别只在 `replace` 会先把旧的全摘掉。原先只做
                        # `add`，理由是"replace 的语义里这些名字都该是现成的"——那条理由
                        # 站不住：主人说「文章 23 的标签整体换成「Rust」「Tokio」」而站内
                        # 没有「Tokio」时，老路是**零写 + 一句"站内没有这个标签"**，主人
                        # 得再说一轮"那你建一个"——正是上一条注里那个"一句话的意图被拆成
                        # 两次来回"，`add` 已经不再是这个形状了，没道理让 `replace` 继续是。
                        # 建出来的名字同 `add`：卡面印着「新建」，主人签字前看得见。
                        # ⚠️ `replace` 还有一格 `[]`——它表示**清空**，不是"没有名字"，
                        # 空列表天然进不了下面那个循环。
                        #
                        # 与值地基的关系（20261009 同步改）：`add` 里的名字照旧走
                        # `_name_arg_fix` 的"校正到主人原话 / 解不出就零写"——那张函数
                        # 此前的 `len(tools) != 1` 早退**已拆掉**，字段取计划里所有 spec
                        # 的并集、取值读 `params`（见 `graph._name_arg_fix` 的长注）。不
                        # 改的话这一族改完 ≥2 条 spec 就等于把值地基整族关掉（⑤「列表值
                        # 也是值」与摘标签两条既有锁当场红，`T070914` 那次"值没有出处"
                        # 的如实拒绝也不再发生）。
                        pname = _write_arg(params.get("parent_tag"))
                        creating: list[str] = []
                        for nm in (list(args.get("add") or []) + list(args.get("replace") or [])):
                            nm = str(nm).strip()
                            if nm and nm not in creating:
                                creating.append(nm)
                        for nm in creating:
                            spec = {"title": nm}
                            if pname:
                                spec["parent_tag"] = pname
                            if hexval:
                                spec["color"] = hexval
                            tools.append(f"create_tag({json.dumps(spec, ensure_ascii=False)})")
                        tools.append(f"set_article_tags({json.dumps(args, ensure_ascii=False)})")
                        # 注记必须把**两件事**都说出来：它进 NOTE 行，narrator 照它组织
                        # 回复（卡面上那几行由 `render_action_lines` 从 specs 印，不走这里）。
                        extra = ("（先建再挂：站内已经有的直接用，没有的先新建——"
                                 "已存在的那几条会在弹卡前自动摘掉）") if creating else ""
                        note = (f"修改文章 {aid} 的标签（只动点名的标签，其余保持不动）{extra}")
        if not note:
            note = f"{workflow.name}：参数齐备"
    else:
        # 通用模板分支：参数**事前校验**（20260925）。此前这一支拿到 PARAMS 就原样展开，
        # 缺参/类型不对要等工具侧 pydantic 报错才发现——那已经是"下一轮"了，白烧一轮
        # 规划，而 planner 从错误帧里也读不出"本技能收哪些参数"。现在缺/坏 ⇒ 零工具 +
        # 一句机器可保证的事实（`param_problem_note`），planner 同轮内就能改对。
        # 写技能各自有更专门的守卫、navigate/content_query 各有自己的判据，都**不**走这里。
        specs = workflow_param_specs(workflow)
        chk = check_workflow_params(workflow, params, specs)
        param_unknown = chk["unknown"]
        if chk["missing"] or chk["bad"]:
            return _param_problem_plan(workflow, chk, specs)
        for tool_name, tmpl in workflow.plan:
            args = expand_template_args(tmpl, params, specs)
            tools.append(f"{tool_name}({json.dumps(args, ensure_ascii=False)})")
    if (not tools and not dropped and not status and not workflow.chat
            and workflow.name != "content_query"):
        # ── 零工具轮不许"不成账"（20260929 批 G，D1）──────────────────────────
        # 现场（trace 20260929 conv258）：主人说「简历日期排到国庆假期以后吧」，
        # planner 选了 `article_status` 却没填 `article_id` ⇒ 写族那一支**只写
        # `note`**（不写 `status`、不设 `param_problem`、`dropped` 也空）⇒ 本函数
        # 返回 `tools=[]` 且记账字段全空 ⇒ `graph.route_after_planner` 只看 TOOLS
        # 行有无 ⇒ **零工具直落 narrator** ⇒ 它对着零帧吐出空文本 ⇒ gate 判
        # `empty_reply` ⇒ 主人收到罐头句「我刚才好像卡住了」。
        #
        # 而"参数不齐"这条通道**早就接好了**：`planner_node` 的同轮纠偏
        # （`param_correct`）与确定性收口（`param_terminal`）读的都是
        # `plan_obj["param_problem"]`，`_param_problem_plan` 就是喂这个键的现成
        # 形状。缺的只是这些出口**没喂键**——它们的注记写得比通用话术还具体
        # （"缺少正文（text）：…如实向主人问清"，见各 `_expand_*`），却停在了一个
        # 没有任何消费方读的字符串里。
        #
        # 落点选在**整条 if/elif/else 的汇聚处**（不是各分支各加一条）：实测
        # `instantiate_plan(技能, {})` 遍历注册表，**28 个技能**会产出这个形状
        # （写族各 `_expand_*` 的守卫出口、article 两件、read_article、
        # review_inbox…），一处判、全族生效。判据收窄到"记账字段全空"，
        # 已有出口（navigate 三值 / `refused` / `_param_problem_plan` 自己）一个都
        # 不受影响——它们本来就写着自己的 status。
        #
        # ⚠️ **`content_query` 硬排除**（不许顺手收进来）：它的 `plan=[]`、两个入参
        # 都可选，"零工具"在**已有帧的收尾轮**上是**合规**的（见该分支上方那段
        # "调用清单为空 = planner 决策无需工具（收尾轮）"的论证），而本函数不知道
        # 有没有帧。收进来会让每一次正常的收尾轮多烧一次 LLM 纠偏。它只走
        # `graph.planner_node` 的 Layer B（那里读得到 `has_frames`）。
        specs = workflow_param_specs(workflow)
        chk = check_workflow_params(workflow, params, specs)
        if not (chk["missing"] or chk["bad"]):
            # 规格全可选 / 参数都在却仍然零工具 ⇒ "没缺没坏"，但这一轮是真的**没有
            # 可执行的东西**（notice_read / message_read 的目标藏在可选参数里、
            # review_inbox 的 `calls` 是空的或逐条都不合格、正文是占位符被展开层挡下）。
            # 不补这一条，纠偏日志与 trace 上会写着"缺=无 坏=无"却一件事都没办。
            # ⚠️ 措辞**不许指认具体原因**——这一支收的是好几种处境，把"可选参数里
            # 没有可办的对象"写死会让占位符那一档的日志说一句错原因（真原因是
            # 展开层挡下了它，注记里写着）。原因在哪，`note` 说。
            chk = {**chk, "bad": ["这一轮系统没能拼出一条可执行的调用"]}
        return _param_problem_plan(workflow, chk, specs, note=note or None)
    # 点名写进了不读清单的技能（20260925 批 C）：**PARAMS.tools / PARAMS.calls 只有
    # content_query 分支读**（见那条 elif 的条件）。planner 把清单写在别的技能里时，
    # 此前的结果是**静默的零执行**——`dropped` 空 ⇒ 剔空纠偏不触发、`drop_terminal`
    # 不触发（它要 `not tools`），planner 以为计划已执行、narrator 照计划声称
    # "我调用了 X"，而 agent.log 里毫无痕迹。这与 20260913 的"白名单静默剔除"是同一族：
    # 计划里写了东西、执行侧没有对应物、中间无人记账。
    # 收进 `dropped` 即复用既有那两条通道（WARNING + trace `rejected_call`；
    # 零工具时同轮内纠偏一次、纠不动就确定性收尾）。**后缀必须是新的**：真话是
    # "工具你够得着、只是这条点名写错了技能"，讲成"你够不到这个工具"是假话，会把
    # planner 往错方向推（同 DROP_SUFFIX_BAD_ARGS 的理由）。
    if not consumed_calls:
        _named: list[str] = []
        _raw = params.get("tools")
        if isinstance(_raw, list):
            _named += [t.strip() for t in _raw if isinstance(t, str) and t.strip()]
        _raw = params.get("calls")
        if isinstance(_raw, list):
            for _c in _raw:
                if isinstance(_c, dict) and isinstance(_c.get("tool"), str) and _c["tool"].strip():
                    _named.append(_c["tool"].strip())
        _suffix = _workflow_no_calls_suffix(workflow.name)
        for _n in dict.fromkeys(_named):        # 同一工具同时写在 tools 与 calls ⇒ 只记一次
            dropped.append(f"{_n}{_suffix}")
    # 状态默认值（`PLAN_STATUS_VALUES`）：走到这里的都是"技能模板展开成功"的轮次，
    # 有工具就是要执行、chat 技能本来就不该有工具。其余零工具的处境各分支已经
    # 显式写过值（navigate 三出口 / `_param_problem_plan`）——空串到这里只剩
    # "非 chat 技能 + 零工具"这一种，含义是"不知道"，消费侧 fail-open。
    if not status:
        status = "executed" if tools else ("answer_only" if workflow.chat else "")
    return {
        "workflow": workflow.name,
        "tools": tools,
        "note": note,
        "reply": workflow.reply_contract,
        "chat": workflow.chat,
        "status": status,
        # 白名单剔除项（只读、不进 plan 文本）：planner_node 据此打 WARNING +
        # trace 事件，让"点名的工具没执行"在日志里可见（20260913 B 项）
        "dropped": dropped,
        # 没人读的参数名（20260925，只读）：planner 以为填了、实际没人消费——与
        # "点名却未执行"同族，同样只记账不阻断（工具照常跑）。见 planner_node 的
        # `planner.param_unknown` 事件。
        "param_unknown": param_unknown,
    }


# 剔空纠偏的"这条例目为什么不合法"后缀（`graph._drop_correction` 认它选话术）已下沉到
# `agent/workflow_model.py`（20261017 批 2 刀 2a）——实例化与展开两层都要拼它。


def _workflow_no_calls_suffix(workflow_name: str) -> str:
    """点名写在 `workflow_name` 里、而该技能不读调用清单。见 `instantiate_plan` 收尾处。"""
    return f"{DROP_SUFFIX_WORKFLOW_NO_CALLS}{workflow_name}：该技能的模板不执行 PARAMS.tools/PARAMS.calls）"


# 技能参数 schema 一族（`arg_type_short` / `arg_enum` / `tool_arg_schemas` /
# `workflow_param_specs` 等）已下沉到 `agent/workflow_params.py`（20261017 批 2 刀 2c）：
# 菜单渲染与事前校验共用那一份规格。本模块顶部 import 回来（迁移期转发）。


def render_workflow_params(workflow: Workflow, specs: dict[str, ParamSpec] | None = None) -> str:
    """planner 菜单里的参数一行：`名字:类型`，必填加 `*`、有默认值加 `=值`、后跟中文说明。

    写法与工具菜单（`graph._menu_arg_signature`）**逐字一致**——两个菜单在 planner
    眼里是同一张表的两段，记号不同会让它两套读法。推不出类型的参数（`any`）不标
    `*`：**宁可少说，不说错**。
    """
    specs = specs if specs is not None else workflow_param_specs(workflow)
    if not specs:
        return ""
    parts = []
    for name, sp in specs.items():
        seg = f"{name}:{sp.type}"
        if sp.required:
            seg += "*"
        elif sp.default is not _NO_DEFAULT and sp.default is not None:
            shown = f'"{sp.default}"' if isinstance(sp.default, str) and sp.default else str(sp.default)
            seg += "=" + shown[:12]
        if sp.desc:
            seg += f"（{sp.desc}）"
        parts.append(seg)
    return "、".join(parts)


# 参数校验与归一（`_contains_ref` / `_coerce_value` / `_bad_choice` /
# `check_workflow_params` / `param_problem_note` / `_param_problem_plan`）已下沉到
# `agent/workflow_params.py`（20261017 批 2 刀 2c）。本模块顶部 import 回来（迁移期转发）。


# 一条 spec 的字面形状：`工具名({json 参数})`。写端在本模块（`tools.append`
# `f"{tool}({json.dumps(args, ensure_ascii=False)})"`，十余处），读端有三处
# （`graph._tool_args`、`tasks.bound_steps`、测试）——**拆分规则只能有一处**，
# 所以留在这里、贴着写端（同族教训见 agent/plan_codec.py `_esc_spec` 的注：
# "改一处要同步十三处"正是全仓审计点名的主导缺陷）。
_TOOL_SPEC_RE = re.compile(r"^(\w+)\((.*)\)$", re.DOTALL)


def split_tool_spec(spec: Any) -> tuple[str, str]:
    """`工具名({json 参数})` → `(工具名, 参数原文)`；**形状不匹配一律 `("", "")`**。

    只做拆分，**不解析参数**：参数原文怎么读由各消费方按自己的容错要求决定——
    `graph._tool_args` 要容模型的漂移（单引号/尾逗号/行注释，见 `_loads_tolerant`），
    而 `tasks.bound_steps` 读的是**本进程刚由 `json.dumps` 产出**的串（严格 JSON 就够）。
    把容错搬到这里会让前者变宽、后者变窄，两边都不对。
    """
    m = _TOOL_SPEC_RE.match(str(spec or "").strip())
    if not m:
        return "", ""
    return m.group(1), m.group(2)


# 实参绑定与模板展开（`args_are_bound` / `expand_template_args` / `check_call_args`）已下沉到
# `agent/workflow_params.py`（20261017 批 2 刀 2c）。本模块顶部 import 回来（迁移期转发）。


# ---------------------------------------------------------------------------
# prompt 注入块构建（planner 提示词注入用）
# ---------------------------------------------------------------------------

def _nav_map_lines() -> str:
    """导航映射表的提示词形态（**分组**，20260926）。

    后台面板那十几个别名单独一行：混进主行后，模型要在三四十个 `别名→路径`
    里挑，而后台名彼此长得很像。分组只是排版——数据源仍是 NAV_MAP 一处
    （后台那组按 DASHBOARD_PANELS 的路径认出来，不维护第二份名单）。
    """
    dash_paths = {p for _, p in DASHBOARD_PANELS}
    plain, dash = [], []
    for alias, path in NAV_MAP.items():
        # IoT 别名在开关关掉时**整条不出现**（不是写成"已下线"）：提示词里出现的
        # 每一个入口都是 planner 可以照着去填 target 的候选，写"→（已下线）"等于
        # 把"本站有这个页面"这件事再讲一遍——`navigate` 那一支的未部署话术是给
        # 用户**主动问起**时用的，不是给 planner 当备选清单用的。
        if not IOT_ENABLED and alias in _IOT_NAV_ALIASES:
            continue
        line = f"{alias}→{path}" if path else f"{alias}→（已下线，如实告知）"
        (dash if path in dash_paths else plain).append(line)
    return ("、".join(plain)
            + "\n后台面板（仅博主可用；用户说「转跳后台/去后台」而没点名板块时，"
              "target 填「后台」即可——那就是后台主页，不要追问是哪个板块）："
            + "、".join(dash))


_NAV_MAP_LINES = _nav_map_lines()


# 系统专用技能名单（`_SYSTEM_ONLY_WORKFLOWS`）、IoT 未装时一并不可见的两件
# （`_IOT_ONLY_WORKFLOWS`）与可见性判据（`visible_workflows`）已随注册表搬到
# `agent/workflow_defs/__init__.py`（20261017 批 2 刀 2b）；本模块顶部 import 回来。
def _workflow_plan_seq(s) -> str:
    """技能的固定工具序列渲染成一行（`工具(参数模板) → 工具(参数模板)`）。"""
    return " → ".join(f"{t}({json.dumps(a, ensure_ascii=False)})" for t, a in s.plan)


def build_planner_context(role: str | None = None, *, slim: bool = False,
                          deny: frozenset[str] | set[str] | None = None) -> str:
    """planner 注入：技能表（触发条件 + 参数 + 工具序列 + 完成判定）+ 导航映射表。

    read_article 不列出——系统快道专用（article_id 是 current_url 解析的系统数据，
    planner 无参可填，误选只能产出 null 工具调用），planner 不可见即不可选。

    `role`（20260921）= 本轮调用者角色（graph 从 principal.known_role 取）：
    技能的 `roles` 非空且不含该角色 → **整个技能不列出来**。非 admin 的管理助手
    技能因此选不出来。传 None（未知身份/老路径/单测）等价于"只有公开技能"——
    失败取向往保守一侧倒，与本仓 authz 的取向一致。

    `slim=True`（20260927）：去掉每条技能的**触发条件/参数/完成判定**三行，只留技能名
    与它的固定工具序列。**20261004 起它是唯一的渲染形态**（`_render_planner_prompt` 的
    `slim_workflows` 默认 True、`planner_node` 也只传这一个值）——那时删掉了文本契约档，
    而"同一份技能表下发两份形态"是 native 独有的处境。`slim=False` 那一支**只剩测试在
    用**（`tests/test_slim_workflows.py` 拿它当对照臂：判据①要求"删掉的必须是 schema 里
    逐字有的"，没有完整那一份就无从比）。**它不是第二个拨盘**：别再给它接任何生产开关。
    理由是这三样在同一轮的 `tools`
    里逐字重复出现（schema 的 `description` = `s.description` + `完成判定：` +
    `complete_when`，`properties` = 同一份 `workflow_param_specs`），而**同一事实写两遍
    是有代价的**：接口层面临的是两份形态不同的同一张表（散文 vs JSON Schema），
    模型得自己判断听哪一份，且两份会漂移——本批落地时就抓到一处：schema 侧的
    `content_query` 描述里原样漏着未展开的 `__无参只读工具清单__` 标记（已修，
    见 `planner_llm.py`）。**不删的**：固定工具序列（`tools` 的 schema 里没有，
    而 `content_query` 的 calls 通道要求写工具名）、导航映射表、能力边界兜底段、
    口语变体说明——这些是这一块独有的信息，删了就真丢。

    ⚠️ `slim` **只影响渲染，不影响任何判据**：谁能选（`visible_workflows`）、参数怎么
    校验（`workflow_param_specs` / `check_workflow_params`）都不看这段文本。

    `deny`（20261007，1d）= **这一轮不许再选的技能名**（来源见
    `agent/block_reasons.py::denied_workflows`：上一轮受阻、且原因是"改参数重试无效"
    那一族）。它与 接口层的 `tools` schema 收的是**同一个集合**——"菜单"横跨
    两处（这张自动生成的技能表 + `tools` schema），只摘一处等于没摘（提示词里还列着，
    模型照着名字点，只是 schema 里没有那个函数了）。**传空集/None 时渲染逐字节不变**，
    无阻碍轮一分钱不花。
    ⚠️ planner 提示词的**判定规则 1** 里还有一份**手写**的「- 技能名：什么时候用它」，
    那一份**刻意不摘**（它不是可点的菜单，只是"这技能干什么用"；真正的闸是 schema）。
    两处一张表、一处散文，各自的取义见 `tests/test_menu_deny.py` 里那条点名用例。
    ⚠️ 它**不是第二份可见性名单**（那件事仍只在 `visible_workflows` 一处判）：它只在
    "上一轮同一个技能刚失败过"时才非空，是**状态**不是**权限**。

    **契约句（"必须怎么做"）与描述同住 `description`**（20261017 起；此前是独立的
    `planner_contract` 字段，20260927–20261017）：当初拆字段的依据是一次五遍 A/B——
    `rag_talk_rag` 完整菜单 5/5 绿 → slim 0/5 红。20261017 四臂重验
    （`eval/contract_position_ab.py`：① 正文 ② 只在 schema ④ 并进 description
    ③ 整句删掉）把它**证伪**了：② 与 ④ 逐条相同（各 84/84），掉的是 ③
    （`pair_one_talks` 1/12）⇒ 位置无关、承重的是那句话本身；原 0/5 的真因是
    **信息丢失**（当时 slim 的 schema 里 `content_query` 的描述原样漏着未展开的占位
    标记，`tools` 参数描述又被 `_SCHEMA_OVERRIDES` 覆盖成没有工具名的一句）——
    归因错在"位置"。字段因此撤掉、并回描述，`tools[]` 回到行业惯例的形状（每个函数
    一个 `description`，没有第二个字段）。**别再拆出来**：拆一次就多守一条"两份会漂"
    的纪律。⚠️ slim 档因此**不再单独渲染契约行**——它随描述一起交给 schema 那一份。
    """
    lines = ["可调用的技能（函数名即技能名，一次选一个；每个技能的触发条件、参数与"
             "完成判定见本轮 tools 里同名函数的 schema，此处不重复，只列它展开成哪些工具）："
             if slim else
             "可用技能（只能从以下技能中选择一个，不得自创步骤或自由编写执行计划）："]
    for s in visible_workflows(role):
        if deny and s.name in deny:
            continue
        if slim:
            # 没有固定工具序列的技能（如 chat）也列一行：它是"可选集合"的一员，
            # 缺了会让模型以为没有这个出口（schema 里有、但这里是同一张表的读法）。
            lines.append(f"- {s.name}：{_workflow_plan_seq(s) if s.plan else '（无固定工具）'}")
            continue
        lines.append(f"- {s.name}：{s.description}")
        # 参数一行 = `名字:类型` + 必填 `*` + 默认值 `=值` + 中文说明（20260925）。
        # 此前这里是 `json.dumps(s.inputs)` ——**只有散文**，必填/类型/默认值一概
        # 看不见，planner 只能靠常识猜，而 `instantiate_plan` 拿到 PARAMS 后什么
        # 都不查（缺参要等工具侧报错，白烧一轮）。记号与工具菜单（`_menu_arg_signature`）
        # 逐字一致：两个菜单在 planner 眼里是同一张表的两段，记号不同会让它两套读法。
        sig = render_workflow_params(s)
        if sig:
            lines.append(f"  参数：{sig}")
        if s.plan:
            lines.append(f"  执行步骤：{_workflow_plan_seq(s)}")
        if s.complete_when:
            lines.append(f"  完成判定：{s.complete_when}")
    # 兜底语义（20260926 洞⑥ 复盘）：这张表此前只有「能做什么」，**没有「做不到怎么办」**。
    # 请求落在表外时（trace 20260926T082919：主人要"给某个用户发个通知"——**当时**通知类
    # 工具只有"读自己的"、站内没有给单个用户发通知的通道），planner 唯一的去处是 chat，
    # 而 chat 的语义是"闲聊"⇒ "这件事我做不到"**整条链路上没有任何一层被要求说出来**，
    # narrator 便用一句承诺句式（"点「确定」我就去办"）把它盖住了。确定性出口在别的维度
    # 早就有先例（NAV_MAP 值为 None = 已下线 ⇒ 如实告知），能力的边界一直没有对应物。
    #
    # ⚠️ 上面那个例子**当天就被建出来了**（技能 notice_send / 工具 send_user_notice）
    # ⇒ 拿它当"站内没有的能力"的举例**现在是假话**，别抄回来。这一段举例只用
    # **已核实真没有**的能力（见下方"登录记录/登录 IP"那一条的依据）。
    #
    # **刻意不写进 chat 的 description**：那会让"没有对应能力"看起来像 chat 的常规用法，
    # planner 可能据此把**真有技能**的请求也判成"做不到"（代价是不动手、主人原地等着，
    # 比多问一句贵）。写在这里，并要求"拿不准就照常选技能"——失败取向往能干活那侧倒。
    lines.append(
        "\n以上技能都不覆盖主人这一轮的请求时（他要办的事站内**根本没有**对应的能力或"
        "通道——例如查某个账号的**登录记录 / 登录 IP / 登录时间**，站内只记账号的"
        "创建时间与角色，没有登录审计）："
        "选 chat，回复里**如实说做不到**，并告诉他你能做的替代是什么（例如公告是全站"
        "可见的）。**拿不准就照常选技能**：把有技能的事判成做不了，比多问一句贵得多。"
        "**禁止**用 chat 收尾去索要「要不要我办／你说一声我就去办」——不排出写操作的"
        "清单就不会有任何确认卡，那句承诺保证是假的（系统只在真排了写操作时才弹卡）。")
    lines.append(f"\n导航映射表（navigate 的 target 参数从这里取值）：\n{_NAV_MAP_LINES}")
    lines.append(
        "口语变体（大小写 IOT/IoT/iot、'设备面板''管理设备'等同义说法）由系统自动归一，"
        "PARAMS.target 直接填映射表中最接近的别名即可，无需自创目标名"
    )
    # 技能描述里的工具枚举按角色展开（20260924）：那些标记是角色相关的，
    # 用哪个角色渲染这条注入，白名单就必须用哪个角色判——两处同一个 role。
    return render_tool_marks("\n".join(lines), role)
