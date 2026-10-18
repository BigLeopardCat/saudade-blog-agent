# -*- coding: utf-8 -*-
"""planner 可点名的工具清单（三条通道，同一个角色口径）。

三张名单各对应 planner 的一条通道：

- `_EXPLICIT_TOOLS_ORDER` —— `PARAMS.tools`（无参点名）。留言/说说/公告/时间/站点信息
  类查询是"一次简单工具调用、无流程"，不成技能，planner 直接点名，`instantiate_plan`
  白名单校验后展开进 TOOLS 行由 execute 确定性执行。
- `_CALLABLE_QUERY_TOOLS_ORDER` —— `PARAMS.calls`（带参调用清单）。检索定位
  （`search_notes` / `rag_search`）、读全文（`get_article_detail`）与数据直取。
- `_ADMIN_QUERY_TOOLS_ORDER` —— 后台读面，**从 `agent.authz` 的 scope 表派生**
  （哪些工具是"后台读面"是安全边界的事实，边界定义在 authz，这里只消费它——
  手抄一份名单必然与边界漂移）。

`explicit_tools` / `callable_query_tools` / `param_tools` 三个取法**都要按角色取**：
菜单是给 planner 看的，而"管理员额外能点到后台读面"这件事必须两条通道同源同角色
（20260924 实测：只有一条放开时，planner 会照着菜单写进另一条，被剔空后重决策，
同一工具跑满 4 个规划轮）。`role=None` 一律取保守一侧。

**写面工具永远不在任何清单里**：它们经技能模板展开，写操作必须过"本轮明确下令"
那道门；本模块只加 `SCOPE_ADMIN_CONSOLE`（只读）那一族。

**为什么单独成模块**（20261017 批 2 刀 2e-1）：这一族**一条出边都没有**——只读
`agent.authz` 的 scope 表与 `agent.principal` 的角色闭集，不碰技能注册表、不碰展开层。
"出边为零"是这一轮拆分的唯一选刀判据（新模块不许 import 回 `agent/workflows.py`，
那会成环；入边靠转发层收着，无所谓）。下一刀（2e-2）的 `agent/workflow_menu.py` 会
import 这里的清单——依赖是单向的 `menu → tools`。
"""

from __future__ import annotations

import agent.authz as authz
from agent.principal import ADMIN_ROLES

# `__all__` = 本模块**所有不以 `_` 开头**的模块级名字（社区惯例，不留例外；
# `tests/test_module_exports.py` 按这条规则机械判着，漂了就红）。
__all__ = ["callable_query_tools", "explicit_tools", "param_tools"]

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
