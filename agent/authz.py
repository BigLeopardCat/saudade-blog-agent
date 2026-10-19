"""权限模型（scope manifest）——"这个身份能做什么"的唯一事实来源（20260920）。

设计取向（与全仓一致）：**能力用声明表达，判据在一个确定性点上**。
  工具清单是 `tools/base.py::_TOOL_REGISTRY`（业务唯一数据源），这里给它配一张
  「工具 → 所需 scope」的声明表；角色 → scope 的授予表也在这里。运行时唯一的
  判据点是 `check(principal, tool)`，由 graph.execute_node 在**调用工具之前**执行
  （与断连检查、参数引用解析同一层：确定性、无 LLM、无一例外）。

现在**默认不拦**（shadow 模式，`AGENT_AUTHZ_ENFORCE=0`）：
  决策照算、照进 trace，但不改变行为。这是收口前的测绘——真实流量里
  跑一段，看"谁在什么时候被拒"，用证据校准授予表，再打开开关。理由同
  `agent_require_assertion` 的滚动上线：先观测、后收口，别拿在途请求做实验。
  唯一的例外是 `must_enforce`：**未验成的身份**（`source="body"`）不进灰度——
  它不是"另一种用户"，是"身份没验成"，没有既有流量可观测（20261019 审计 SAU-005）。

失败取向（enforce 打开后）：
  - 未知角色（role=None / 不在 KNOWN_ROLES）→ **零权限**。从不"默认放行"，
    也从不"默认当管理员"。
  - 未声明的工具 → **拒绝**（fail-closed），并由 tests/test_authz.py 的完备性断言在
    CI 层拦住——新增工具忘了声明是工程疏漏，不该靠运行时宽容。
  - 拒绝的形态复用既有 blocked 链路（`__ERROR__` 帧 + `scope_denied` 原因码）：
    planner 如实收尾、reflector 受限复盘，不新增决策分支，也不静默吞掉。

跨语言契约：角色名与 scope 名与 Rust 侧 `src/authz.rs` 必须一致（roles 是
`user.role` 列的取值域，scope 是两边的共同词汇表）。改一侧须同步另一侧 +
两侧各自的单测（agent: tests/test_authz.py / rust: authz.rs 尾部 #[cfg(test)]）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import NamedTuple

from agent.principal import (KNOWN_ROLES, ROLE_ADMIN, ROLE_SUPERADMIN,
                            ROLE_USER, ROLE_ZAKO, SOURCE_BODY, Principal)

# ── scope 词汇表 ─────────────────────────────────────────────────────
# 命名 = <动作>.<对象>。对象轴现在只有「谁的」这一维（public / own），
# 第二维（哪类资源）等真有第二个消费者再加。
SCOPE_READ_PUBLIC = "read.public"      # 公开内容：文章/标签/分类/留言/说说/公告/站点信息
SCOPE_READ_OWN = "read.own"            # 自己的私有数据：自己的会话历史、自己的设备
SCOPE_WRITE_PAGE = "write.page"        # 作用于访客自己看到的页面：导航/特效/夜间模式
SCOPE_WRITE_DEVICE = "write.device"    # 物理世界写操作：IoT 设备（当前唯一：屏幕刷字）
SCOPE_ADMIN_CONSOLE = "admin.console"  # 后台管理面**读**（Rust auth_guard 后面的东西）
SCOPE_WRITE_CONSOLE = "write.console"  # 后台管理面**写**（标签/文章状态——20260921 第二轮新增）
# 用户**自己**的私有数据写（20260923 第七轮：收藏文章、标记通知已读）。
# 与 write.page 的区别：页面/特效写的效果就在用户眼前那一屏上（改错了立刻看得见也
# 立刻能改回来），而收藏与已读是**落库的私有状态**（刷新还在）；与 write.console
# 的区别：它只动**自己的**私有数据——不外显、不碰别人的东西，所以不需要后者那两道
# （`_HARD_SCOPES` 硬拦、`_ALWAYS_CONFIRM_TOOLS` 一律弹窗）。
SCOPE_WRITE_OWN = "write.own"

ALL_SCOPES = frozenset({
    SCOPE_READ_PUBLIC, SCOPE_READ_OWN,
    SCOPE_WRITE_PAGE, SCOPE_WRITE_DEVICE, SCOPE_WRITE_OWN,
    SCOPE_ADMIN_CONSOLE, SCOPE_WRITE_CONSOLE,
})

# 写操作：留给后续"人在回路确认"挂钩（见 docs/identity-and-permissions.md 的前置需求 ③）
WRITE_SCOPES = frozenset({SCOPE_WRITE_PAGE, SCOPE_WRITE_DEVICE,
                          SCOPE_WRITE_OWN, SCOPE_WRITE_CONSOLE})

# ── 不吃 shadow 开关的 scope（20260921）────────────────────────────────
# shadow 模式（`enforcing()` 默认 False）的存在理由只有一个：**观测既有流量**，
# 用真实拒绝率校准授予表，再决定收口——它假定被观测的能力**本来就在跑**。
# `admin.console` 不满足这个前提：它是 20260921 才第一次有工具声明的**纯新增能力**，
# 历史流量里一条都没有，没有"观测期"可谈；而在 shadow 期放行等于"谁问都给"，
# 恰恰是这套模型要防的事。所以它硬拦——判据本身不打折，只是不参与灰度。
# （同源先例：写操作的 consent 闸也不吃 shadow，见 graph.execute_node。
#   区别在于 consent 回答"这一次要不要做"，这里回答"这个人能不能做"。）
#
# 20260921 第二轮把 `write.console` 一并加进来，理由是**同一个门的两面**：后台
# 写入与后台读取在 Rust 侧走的是同一道 `auth_guard`，没有"观测期"这回事。这里
# 尤其危险的是：若只加 CONSENT_SCOPES 不加 _HARD_SCOPES，生产环境
# `authz_enforce=False` 会让 `decision.allowed=False` 的非 admin **直接落到 invoke**
# （graph.execute_node 的 `not allowed and not enforcing` 分支只记录不拦）——即
# "有确认语的非管理员能把文章设成私密"。tests/test_admin_write.py 有专门一条断言锁它。
_HARD_SCOPES = frozenset({SCOPE_ADMIN_CONSOLE, SCOPE_WRITE_CONSOLE, SCOPE_WRITE_DEVICE})
# `write.device`（20261019 安全审计 SAU-007，CWE-284）：它与上面两条**不同族**——
# 那两条的入选理由是"纯新增能力、没有观测期可谈"，而这一条的理由是**后果落在物理世界**：
# 它唯一的消费方是 `device_oled_display`（把一段字刷到设备屏上）。
# shadow 的语义是"只记不拦、用真实拒绝率校准授予表"，它默认**被观测的能力本来就在跑、
# 放行错了顶多多做一件小事**；而这里放行错了的代价是一条谁也收不回的物理动作，
# 且它是在**身份没验成**的情况下被放行的（回退身份 `role=None` 时本表判零权限，
# 只有 shadow 会把它放过）⇒ 这一类不该有灰度期。
# ⚠️ 与 SAU-005 的分工：那条修的是"回退身份"（出厂开关 `AGENT_REQUIRE_ASSERTION`），
# 这一条是**独立的一道纵深**——两个开关各关各的，任一条失效另一条仍然拦得住。
# 实测（20261019）：全部生产 trace 里 `authz_shadow` 事件 **0 条** ⇒ 真实流量中从未出现过
# 任何 scope 的拒绝，而三档角色都授予 `write.device` ⇒ 对正常用户**一字不改**，
# 它会拦住的只有"身份不明"那一种（那本来就是本表要表达的零权限）。
# 20260923 刻意**没有** `write.own`：那张表的判据是"纯新增能力、历史流量里一条都没有、
# 没有观测期可谈"，而"给自己收藏一篇文章"恰恰是**每个登录用户本来就能在页面上做的事**
# （收藏按钮、个人中心），工具只是换了只手来做 ⇒ 它满足观测期的前提，进 shadow 是正当的。
# 方向也相反：放行错了的代价是"多了一条自己的收藏"（自己看得见、一键能取消），
# 而不是"网站对外可见状态被改了"。`write.own` 的写面守卫靠下面三道（都确定性）：
#   ① scope 本身（三个角色都授予，但没有身份就没有 uid，工具层 uid<=0 直接不发请求）
#   ② 同意闸（判"本轮有没有明确命令"，见 `_own_command`）
#   ③ 工具层的写后复核（复核不出那一行就报 unavailable，不谎称成功）
#
# `read.own` / `write.own` 20261019 复核（安全审计 SAU-007 的"隐私面"那半 + SAU-005
# 的越权链）——**结论仍是留在表外**，但上面那条理由里有一句是错的，改掉：
#   原文写"硬拦只是把同一个结局换一个原因码，买不到新防线"。那是按**已收口**（enforce）
#   的情形推的；在 shadow 档下，deny 只是记一笔、工具**照常执行** ⇒ 结局根本不同。
#   真正的分界不是"能不能多拦一次"，而是**这条 deny 有没有真实可达的路径**：
#     · `read.own`：三个真角色全授予 ⇒ 能拒的只有 `zako`（`CHAT_ONLY_ROLES`，planner
#       顶层短路 ⇒ 整轮零工具，够不着）与**未验成的身份**；
#     · `write.own`：同上，且它写的是调用者自己的私有数据。
#   ⇒ 所以堵口不在"把 scope 拉进本表"，而在**"未验成的身份一律硬拦"**那一条（见
#   `must_enforce`）：它精确覆盖了这两条 deny 的唯一真实可达路径，且不牵连
#   `source="golden"` 的评测身份（golden 的用例默认 `role=None`，若按 scope 硬拦，
#   一整族既有用例会从"shadow 放行"变成 BLOCK —— 那是**判据基线**的移动，不该由一次
#   安全加固顺带做掉）。审计那一条给的入选理由是"与 write.console 同属纯新增能力、
#   无既有流量可观测"——对 `read.own` 不成立（自己的收藏/通知/会话历史是**早就在跑**的
#   既有能力，观测期正是给它设的），这一处分歧连同理由记在 PR 正文里。

# ── 要不要把"谁执行的"写进回执（20260921）─────────────────────────────
# 回执会经 execution_log.detail 落生产库、并被下一轮的 recent_executions 注入
# narrator 的上下文。只对**后台写**标注执行身份：访客问一句"帮我跳转到首页"，
# 回执上写"以访客身份"既无信息量又是把 uid/角色往库里塞。写操作不同——它是
# 审计的一部分（用户拍板：零迁移，审计走 detail）。
AUDIT_SCOPES = frozenset({SCOPE_WRITE_CONSOLE})

# ── 角色 → 授予 ──────────────────────────────────────────────────────
# 纪律：**授予表必须覆盖该角色当前用得到的全部工具**，否则 shadow 期给出的拒
# 绝会是我们自己配错，而不是真实越权。user 一档刻意保留 write.device——
# 设备归属由 device-service 按 uid 校验（tools/base.py 现签用户 JWT），这是既有
# 事实；本表的问题不是"是否该给"，而是"给的时候有没有被写下来"。
#
# `write.own`（20260923）三档都授予：它写的是**调用者自己的**私有数据，而三个角色
# 都是"网站用户"，都有权收藏文章、把自己的通知标已读——角色轴在这里没有信息量
# （user 与 admin 在这件事上完全同权），真正区分的是"以谁的 uid 去写"，由工具层
# 按本轮发起人身份落地（uid<=0 直接不发请求）。
_ROLE_SCOPES: dict[str, frozenset[str]] = {
    ROLE_USER: frozenset({
        SCOPE_READ_PUBLIC, SCOPE_READ_OWN, SCOPE_WRITE_PAGE, SCOPE_WRITE_DEVICE,
        SCOPE_WRITE_OWN,
    }),
    ROLE_ADMIN: ALL_SCOPES,
    # 超管（20260926）：管理员的**超集**。授予上不新开一档——它多出来的那部分不是
    # "更多 scope"，而是策略上的豁免（谁能被冻/被改身份），那个只在 Rust 侧实现一次
    # （src/authz.rs 的账号管理策略），agent 侧不复制。
    ROLE_SUPERADMIN: ALL_SCOPES,
    # 杂鱼（20261002）：**零授予**。这一格不是"还没配"，是结论——杂鱼按设计不调用
    # 任何工具（能力边界真正的硬保证在 graph.planner_node 的短路，见 CHAT_ONLY_ROLES）。
    # 写出来是为了让"这是刻意的零"可见，而不是靠 `_ROLE_SCOPES.get(role, 空集)` 的
    # 兜底——兜底与刻意长一样，出问题时没人分得清是漏配还是设计。
    ROLE_ZAKO: frozenset(),
}

# ── 工具 → 所需 scope（**完备性是硬要求**：见 tests/test_authz.py）───────────
# 键 = 工具名（与 _TOOL_REGISTRY 一一对应）；值 = 单个 scope。
# 一个工具要多个 scope 的情况现在没有，出现时改成 tuple 并同步 check()——
# 不为想象中的需求先把判据复杂化。
TOOL_SCOPE: dict[str, str] = {
    # 公开只读（数据来自公开网页接口）
    "list_notes": SCOPE_READ_PUBLIC,
    "search_notes": SCOPE_READ_PUBLIC,
    "get_article_detail": SCOPE_READ_PUBLIC,
    "rag_search": SCOPE_READ_PUBLIC,
    "get_top_notes": SCOPE_READ_PUBLIC,
    "list_categories": SCOPE_READ_PUBLIC,
    "list_tags": SCOPE_READ_PUBLIC,
    "get_announcements": SCOPE_READ_PUBLIC,
    "list_guestbook": SCOPE_READ_PUBLIC,
    "list_talks": SCOPE_READ_PUBLIC,
    "get_blog_info": SCOPE_READ_PUBLIC,
    "get_social_links": SCOPE_READ_PUBLIC,
    "get_site_map": SCOPE_READ_PUBLIC,
    "search_knowledge_base": SCOPE_READ_PUBLIC,
    "get_current_time": SCOPE_READ_PUBLIC,
    "get_weather": SCOPE_READ_PUBLIC,
    # 自己的私有数据（服务端按 uid 过滤，见 tools/base.py）
    "get_chat_history": SCOPE_READ_OWN,
    "list_devices": SCOPE_READ_OWN,
    # 收藏 / 未读汇总 / 站内通知（20260923）：同样只读自己那一份
    # （Rust `/api/protected/favorites|notifications*` 全走 auth_uid）。
    # 三档角色都有这一档，匿名没有——匿名时工具层直接不发请求并如实说未登录。
    "list_my_favorites": SCOPE_READ_OWN,
    "get_unread_summary": SCOPE_READ_OWN,
    "list_notifications": SCOPE_READ_OWN,
    # 自己的信箱（20260923 批 8）：同一档——Rust `/api/protected/messages` 只认
    # auth_uid，**没有"读别人的信箱"的接口**。
    "list_my_messages": SCOPE_READ_OWN,
    # 自己的河灯留言（20261008）：Rust `/api/protect/board/mine` 只认 auth_uid，
    # 返回**本人全部**河灯（含待审/未通过）——与公开池 `list_guestbook`
    # （scope=read.public、恒只有已通过）是同一条数据的两个面，缺了这一面就答不了
    # "我哪条留言通过了 / 我放过的灯有哪些"。
    "list_my_board": SCOPE_READ_OWN,
    # 用户自己的数据·**写**那一半（20260923 批 7）：收藏 / 取消收藏 / 标记已读。
    # 写的是同一个人的同一份数据，所以 scope 是 read.own 的写方向 `write.own`：
    # 三档角色都有、匿名没有、**不进 `_HARD_SCOPES`**（代博主收藏是正当的
    # ——它写的是发起人自己的账号，不是第三方的数据）。
    # 但进 `CONSENT_SCOPES`：判据见本文件 CONSENT_SCOPES 上方那段（agent 不能
    # 自己判断"这篇值得收藏"就替用户收藏）。
    # 与后台写同一个区别：不改对外可见状态。收藏不外显；"标记已读"更不可逆
    # （`is_read` 撤不回，见 tools/base.py 的 read_notifications 与 batch 记录）。
    "add_favorite": SCOPE_WRITE_OWN,
    "remove_favorite": SCOPE_WRITE_OWN,
    "read_notifications": SCOPE_WRITE_OWN,
    # 标记**收到的**信已读（20260923 批 8）：与标记通知已读是同一件事的不同物件，
    # 同一档 scope（写的是自己账号里的已读态，不外显、不碰别人的东西）。
    # **发信不在这张表里**——它写进的是**别人的**收件箱，见"站内信·发信"那一批。
    "read_messages": SCOPE_WRITE_OWN,
    # 作用于访客自己的页面
    "navigate_to": SCOPE_WRITE_PAGE,
    "toggle_effect": SCOPE_WRITE_PAGE,
    "toggle_dark_mode": SCOPE_WRITE_PAGE,
    # 物理世界写操作
    "device_oled_display": SCOPE_WRITE_DEVICE,
    # 管理助手（20260921）：报表类只读工具，但**数据来自后台管理面**——
    # 它们读的是 Rust `auth_guard` 后面的东西（留言审核视图、全站用户统计），
    # 以及本机的服务/磁盘/日志。scope 取 admin.console：读**运维面**是博主本人的事
    # （见 docs/identity-and-permissions.md）。
    "get_server_status": SCOPE_ADMIN_CONSOLE,
    "get_service_health": SCOPE_ADMIN_CONSOLE,
    "get_moderation_status": SCOPE_ADMIN_CONSOLE,
    "get_user_stats": SCOPE_ADMIN_CONSOLE,
    # 文章流量报表（20260930）：与上面三张同门——数据在 `protected_routes` 后面
    # （Rust `auth_guard` 只放 admin 进来），agent 以发起人身份代调。
    "get_note_stats": SCOPE_ADMIN_CONSOLE,
    # 文章分期报表（20261001）：同门，读的是 `.../stats/notes/periods`（后台数据
    # 统计页「周报/月报/年报」那个页签的同一个端点）。与 get_note_stats 是两张纸：
    # 那张是当下快照，这张按期切开。
    "get_note_periods": SCOPE_ADMIN_CONSOLE,
    # 管理助手（20260921 第二轮）：后台**写**。<动作>.<对象> 与 admin.console
    # 成对：一个是这道门的读方向，一个是写方向。
    #   `list_admin_notes` 取 admin.console——它读的是**后台**
    #   文章列表（含草稿/私密），与上面四个报表工具同一个门；没有它，"把草稿
    #   发布出来"这条指令在 planner 侧拿不到 id（公开接口一律滤 is_public）。
    #   三个写工具取 write.console：进 _HARD_SCOPES（不吃 shadow）+ 进
    #   CONSENT_SCOPES（每次要命令式确认）。后台写与 admin.console 同域，
    #   Rust 那道门只认 admin。
    "list_admin_notes": SCOPE_ADMIN_CONSOLE,
    # 后台**留言名册**（20261001）：与 `list_admin_notes` 完全同门——读的是
    # `GET /api/protect/board`（Rust `auth_guard` 之后），一样是"后台只读面"。
    #   它补的是一个**结构性缺口**：公开留言接口看不到发表账号（那里透出的
    #   `author` 是留言人自己填的自由文本，留空即匿名），于是"这条是谁发的"在
    #   公开视图里无解；而后台视图的每一行都带 userId/username/nickname，且放灯
    #   强制登录 ⇒ 匿名留言同样溯得到。取 admin.console 而不是 read.public：
    #   账号信息是对外不公开的后台事实，越权读到的后果与读草稿文章同级。
    "list_admin_board": SCOPE_ADMIN_CONSOLE,
    "create_tag": SCOPE_WRITE_CONSOLE,
    "set_article_status": SCOPE_WRITE_CONSOLE,
    "set_article_tags": SCOPE_WRITE_CONSOLE,
    # 第四轮（20260921）：标签改/删 + 分类增删改。与上面三个写工具同门——
    # 后台数据、对外可见状态、改完不自动复原（删标签还会连带摘掉文章上的引用）。
    # 同样不进 planner 的任何点名白名单，只能由技能模板展开。
    "update_tag": SCOPE_WRITE_CONSOLE,
    "delete_tag": SCOPE_WRITE_CONSOLE,
    "create_category": SCOPE_WRITE_CONSOLE,
    "update_category": SCOPE_WRITE_CONSOLE,
    "delete_category": SCOPE_WRITE_CONSOLE,
    # 第五轮（20260922）：站内公告代发/改/删。同门——公告是**对全体访客可见**的
    # 文字，发错了收不回（删除也没有回收站），正是"离开用户眼前、以他名义对其他
    # 人可见"的典型，所以既要 write.console 也要每次命令式确认。
    "create_announcement": SCOPE_WRITE_CONSOLE,
    "update_announcement": SCOPE_WRITE_CONSOLE,
    "delete_announcement": SCOPE_WRITE_CONSOLE,
    # 第六轮（20260922）：河灯留言的人工复核（通过/驳回）与删除。同门——改的是
    # **别人的**留言在站内的可见性，且主人不在那个页面上（后台管理视图里看不到
    # 那条留言，除非他去翻）。驳回是可改判的，删除不是。
    "audit_board_comment": SCOPE_WRITE_CONSOLE,
    "delete_board_comment": SCOPE_WRITE_CONSOLE,
    # **文章评论**那一族（20261019）：与上一族逐条同门、同一个理由（改的是**别人的**
    # 文字在站内的可见性，而且主人多半不在那个页面上）。两张表是两套东西——
    # 留言在 `/guestbook`（后台 `?tab=review`），评论长在文章正文下面（后台
    # `?tab=comment`），工具名也分开，**别把其中一族的 scope 挪给另一族**。
    #   名册那条取 admin.console：它读 `GET /api/protect/comments`，与 `list_admin_board`
    #   同一道门（后台只读面，带发表账号等对外不公开的事实）。
    "list_admin_comments": SCOPE_ADMIN_CONSOLE,
    "audit_comment": SCOPE_WRITE_CONSOLE,
    "delete_comment": SCOPE_WRITE_CONSOLE,
    # 第七轮（20260926）：后台首页的待办 / 日程。**读那条**取 admin.console——
    # 它读的是 `/api/protected/todos`（Rust `auth_guard` 之后，只有管理员拿得到），
    # 与上面四个报表工具同一道门；**追加那条**取 write.console，因为 `/api/protected/
    # todos/item` 同样在守卫域内。
    #   为什么不取 read.own / write.own：那张列表**按用户**存（uid 过滤），看着像
    #   "自己的数据"，但接口本身挂在 admin 守卫后面——普通登录用户在前端也打不开
    #   后台首页。取 own 会让授权层对普通用户说"允许"，而 Rust 那道门随后 403：
    #   一道会说谎的授权层比一道更严的授权层糟糕得多（同 `list_admin_notes` 的取舍）。
    "list_dashboard_todos": SCOPE_ADMIN_CONSOLE,
    "create_dashboard_todo": SCOPE_WRITE_CONSOLE,
    # 第十轮（20260926）：把某一条待办勾成完成。同一道门（`/api/protected/todos/done`
    # 在 `auth_guard` 之后）——**它是这一族里唯一"改已有东西"的一件**，所以要按写
    # 对待而不是按读：漏了这里的后果是静默的（`required_scope` 取不到 ⇒ 授权层放行
    # 一件没人声明过的写，见 tests/test_authz.py 的"注册表全量都声明了 scope"）。
    "complete_dashboard_todo": SCOPE_WRITE_CONSOLE,
    # 第十二轮（20260929 批 G）：改某一条待办的**排期日**（新端点
    # `POST /api/protected/todos/date`，同样在 `auth_guard` 之后）。与上一件同一族
    # （动的是清单上已有的那一行），区别只在改的是哪一列——所以判决、卡面纪律、
    # 幂等那条路都跟着上一件走。
    "reschedule_dashboard_todo": SCOPE_WRITE_CONSOLE,
    # 第九轮（20260926）：冻结 / 解冻后台账号。同门（write.console）——改的是
    # **第三方的登录能力**（被冻的人当场被踢下线，且解冻也换不回他那批会话），
    # 后端那两条路由同样挂在 `auth_guard` 后面（见 src/routes/temp_user.rs）。
    "freeze_account": SCOPE_WRITE_CONSOLE,
    "unfreeze_account": SCOPE_WRITE_CONSOLE,
    # 第十三轮（20261002）：变更账号身份（下放给管理员）。同门（write.console）——
    # `POST /api/temp-users/:id/role` 同样挂在 `auth_guard` 之后，动的是**第三方账号
    # 的权限档位**（且成功后 `token_version + 1`，他当场被踢下线）。
    # 与冻结族的一处结构差别要留意：**能改谁、能改成什么 agent 侧一个字都不判**
    # （用户拍板：政策只有一份实现，在 `src/authz.rs::check_role_change`），
    # 所以这里给的是一道"能不能进这扇门"的 scope，不是一个权限矩阵。
    "set_account_role": SCOPE_WRITE_CONSOLE,
    # 第十四轮（20261002 内容风控下放）：禁言 / 解除禁言一个后台账号。同门
    # （write.console）——`POST /api/temp-users/:id/mute` 也挂在 `auth_guard` 之后，
    # 动的是**第三方的发言能力**（他照常登录浏览，只是发不出评论与留言）。
    # 与冻结族的两处结构差别要留意：①后端**只写 `muted_until` 一列**，绝不改
    # `status`、绝不 bump `token_version`（所以后果话术两族不通用，见 authz 上方
    # `_ALWAYS_CONFIRM_TOOLS` 那条与 `adminops._MUTE_*`）；②方向在**工具名**里
    # （`account_mute` / `account_unmute`），不是参数。
    "account_mute": SCOPE_WRITE_CONSOLE,
    "account_unmute": SCOPE_WRITE_CONSOLE,
    # 第十一轮（20260926）：给**单个账号**发站内通知。同门（write.console）——
    # `POST /api/temp-users/:id/notice` 也在 `auth_guard` 之后。它与公告族是同一件
    # 事的两种影响面：公告是**对全体访客**说的话（announcements.rs 逐行展开），
    # 这一条是**对某一个人**说的话（notice.rs）——收件人只有一个，但仍然是"以主人
    # 名义对第三方说出去的一段字，且没有撤回通道"。
    "send_user_notice": SCOPE_WRITE_CONSOLE,
    # 第十二轮（20260929）：对话额度的申请队列与三个写。**读那条**取 admin.console
    # （`GET /api/protected/quota/requests` 在 `auth_guard` 之后，与上面几个报表
    # 同一道门）；**三个写**取 write.console——批准/驳回走
    # `POST /api/protected/quota/requests/:id/review`，主动重置走
    # `POST /api/temp-users/:id/quota-reset`（账号族那一行，与冻结/通知同一道门）。
    #   为什么不取 write.own：额度是**别人的**额度，不是主人自己的数据。
    #   为什么主动重置不新开一条路由而挂在账号族下：它复用 `authz::is_listable_role`
    #   那道目标守卫（超管结构上够不着，与 `send_user_notice` 同一条论证），
    #   而且账号管理页那一行本来就住在那个族里。
    "list_quota_requests": SCOPE_ADMIN_CONSOLE,
    "approve_quota_request": SCOPE_WRITE_CONSOLE,
    "reject_quota_request": SCOPE_WRITE_CONSOLE,
    "reset_user_quota": SCOPE_WRITE_CONSOLE,
    # 第十三轮（20261006）：读**后台账号名录**。取 admin.console——它读的就是
    # `GET /api/temp-users`，与上面冻结/通知/额度那一整族**同一道门、同一个端点**
    # （那几件写工具内部的 `_user_directory` 调的就是它），Rust 侧仍是 `auth_guard`
    # 只认 admin。
    #   它为什么必须存在：那一族**写**技能的参数契约都写着「账号名必须能在后台账号
    #   列表里看到」，而在本轮之前**没有任何一个技能读得出那份列表**——planner 够不着
    #   这条前提，只能去抓一个近邻（trace `20261006T082437`：主人说「给本本恢复身份」，
    #   planner 点了 `list_admin_notes` = 文章清单）。读侧取 admin.console 的理由
    #   与 `list_admin_notes` 逐字相同：它读的是**后台**视图。
    "list_accounts": SCOPE_ADMIN_CONSOLE,
}


# ── 写操作的「人在回路」确认（20260920）──────────────────────────────
# 分工：**权限**回答"这个人能不能做"，**确认**回答"这一次他到底要不要做"。
# 只有**离开了用户自己眼前**的写入才需要确认：页面/设备写操作的效果就发生在
# 用户眼前（他立刻看得见、也立刻能改回来），所以不进这张表。
#
# `write.console`（20260921）：后台写改的是**对外可见状态**（一篇文章从公开变私密，
# 读者立刻打不开），且改完不会自动复原。
# `write.own`（20260923）：它**不满足**上面那条"离开用户眼前"的措辞（收藏与已读都
# 只在自己账号里，不外显、可逆），但满足那条措辞背后的**真判据**——"这一次到底要不要做"
# 需要一个确定性答案。反过来说：agent 自己判断"这篇值得收藏"就替用户收藏了，是**以用户
# 的名义往他账号里写状态**，是同一类错（只是影响面小）。所以仍要一句明确的命令；
# 判不出来时走既有弹窗（`graph._confirm_popup`），不是硬拒。
CONSENT_SCOPES = frozenset({SCOPE_WRITE_CONSOLE, SCOPE_WRITE_OWN})

# **一律弹窗**的那几个工具（20260922 第五轮，用户点名要求）：
# 「可以代发公告，但是内容也需要**弹窗等待管理员确认**」。
#
# 为什么不是"通常那样"就够：别的后台写有第二条路——用户这句要是写成了明确命令
# （句首"把/新建…"+ 明确目标），同意闸判命令成立、**当轮直接执行、不弹窗**。这条路
# 对文章/标签/分类是合理的（他确实在下命令），但公告不同：它是**对全体访客说的话**，
# 而且是 agent 替主人起草的内容——错一个字就是主人对外说过那句话。所以公告三件把
# 那条捷径**在结构上关掉**：`consent_granted` 对它们恒 False ⇒ 永远走弹窗，
# 主人在弹窗里看见标题与正文预览再点确定。多问一次的成本远低于一次说错的公告。
#
# 注意这**不是**权限判据（`check` 不看这张表）：非管理员来问，走的仍是 scope 拒绝，
# 弹窗都到不了（见 graph._confirm_popup 的顺序）。
_ALWAYS_CONFIRM_TOOLS = frozenset({
    "create_announcement", "update_announcement", "delete_announcement",
    # 20260922 第六轮补**删留言**：动的不是主人的东西——那是**访客写下的内容**，
    # 而且删了没有回收站（`delete_board_comment`）。
    "delete_board_comment",
    # 20260929 批 H 补**留言复核**（20260922 时它刻意**不在**这张表，那条论证是"它
    # 可改判、且只改可见性，与 set_article_status 同类，走命令式措辞免问那条路"）。
    # 批 H 把它的目标从"主人原话里那段引文"换成**台账里的 talkId**——那条捷径的前提
    # 随之没了：免问的前提是"这句话是主人下的命令"，而命令里**没有目标**（编号是
    # 系统印的、他本人认不出），判据能核对的只剩"他是不是在让我复核"这半，另一半
    # 得靠人眼在卡上对（卡面印着那条留言的原文与现状）。同族取向见冻结族那条：
    # 判得出来不等于该免问，而这里连判都判不完整。
    "audit_board_comment",
    # 20260926 补**加待办/日程**：它写的是主人**自己**那份列表（不外显、可删可改），
    # 单看后果够不上前两条那种"收不回"。进这张表的理由是**判据的形状**：这是唯一
    # 一件"目标由主人随口描述、没有站内既有名字可核对"的写入（"记一条：周五交房租"）
    # ——命令式措辞与内容描述在这句话里长得一模一样，`_console_command` 判不出"他
    # 到底是在让我加，还是在跟我聊这件事"。而漏判的代价不是"多做一件小事"，是
    # **凭空往他的列表里塞一条他没让记的东西**。判不出来就别判：结构上每次都弹卡。
    "create_dashboard_todo",
    # 20260926 补**冻结/解冻账号**（用户拍板：每次都弹卡，不留任何捷径）。
    # 进表的理由与上一条同族但更硬：它动的**不是主人的东西**——后果落在**第三方
    # 的登录能力**上（一个活人当场被踢下线，而且解冻也换不回他那批会话，
    # `token_version` 只增不减，见 scripts/probe_token_revoke.py）。命令式措辞
    # （「把 guest5 冻结掉」）在这件事上判得出来，但**判得出来不等于该免问**：
    # 漏判的代价不是"多做一件小事"，是把一个活人踢下线且不可复原。
    # 顺带一提，这也是"管理员之间不可互冻 / 超管谁都不能冻"那两条后端规则的
    # 人眼复核点：卡面把账号名与现状印出来，主人点确定前能核对"是不是那个人"。
    "freeze_account", "unfreeze_account",
    # 20260926 补**勾完成待办**（用户拍板：每次都弹卡）。进表的理由与加待办那条
    # 同族：待办正文是主人随口说的一句话，`_console_command` 判不出"他是在让我勾"
    # 还是"他在跟我说这件事办完了"（「那个买菜的搞定了」——句首没有命令骨架、
    # 目标是自由文本）。而这个方向的漏判**比加一条更贵**：勾错的那一行在列表上
    # 看起来和勾对的一模一样（没有 id 也没有日志给他看），他可能几天后才发现
    # 那件没做的事被记成了做完。
    "complete_dashboard_todo",
    # 20260929 补**改待办排期**（批 G，与勾完成同族）：进表的理由与它逐字相同——
    # 目标是一段自由文本，判不出"他是在让我改日子"还是"他在跟我提这件事"；而改错
    # 那一行的排期比勾错更隐蔽：日程会静静躺在后台日历的**错误格子**里，他不到那天
    # 不会发现（同 `tools.base._TODO_CLEAR_DATES` 引的那条注）。卡面必须让他核对
    # "改的是哪一条、现在排在几号、要改成几号"三样。
    "reschedule_dashboard_todo",
    # 20260926 补**给单个账号发通知**（用户拍板：每次都弹卡，不留任何捷径）。
    # 进表的理由与公告三件同源，但指向的是**某一个第三方**而不是全体访客：正文是
    # agent **替主人整理**的一段话（用户明确拍板允许整理、不必逐字），而它直接落在
    # **别人的**个人中心里、站内没有删除已发通知的功能 ⇒ 说错一个字就是主人对某个人
    # 说过那句话，收不回。命令式措辞（「给他发个通知：…」）在这件事上判得出来，
    # 但判得出来不等于该免问：漏判的代价不是"多发一条"，是**以他的名义对第三方
    # 发了一段他没细看的话**。
    # 人眼复核点 = 卡面把**正文全文**印出来（`adminops.render_notice_action`）。
    "send_user_notice",
    # 20260929 补**额度申请的三件**（批准 / 驳回 / 主动重置）。三件都进表，理由各不
    # 相同，而差异点必须落在**后果**上（这一族的 why 是"主人点确定前唯一读得到的
    # 那句后果"）：
    #   · **批准** = 把额度再给他 500 轮（这一下**不可撤销**：额度是个计数器，
    #     没有"撤回"这个概念），且他立刻能继续提问；
    #   · **驳回** = 唯一"什么都不改变"的一件，而且落点在**第三方**身上：
    #     他会收到一条以主人名义发出、没有撤回通道的站内通知（同 `send_user_notice`
    #     那半），额度一个字节都不动；
    #   · **主动重置** = **不需要他申请过**（"你替他决定"而不是"回应他的申请"）——
    #     这一件与前两件最容易被读混，所以它进表的理由与冻结族同一条：命令式措辞
    #     在这件事上判得出来，但判得出来不等于该免问——漏判的代价是把一个**没有
    #     申请过**的人的额度清零，而这件事没有任何人眼复核点（他不申请就不会知道）。
    "approve_quota_request", "reject_quota_request", "reset_user_quota",
    # 20261002 批 J 补**变更账号身份**（下放给管理员那一件）。进表的理由与冻结族
    # 同源（动的不是主人的东西：改的是**第三方**的权限档位，且成功后 `token_version+1`
    # ⇒ 他当场被踢下线），但**差异落在后果上**，而后果句是主人点确定前唯一读得到的那句：
    #   · 冻结 = 单向的"关掉"，说清了就是损失；
    #   · 变更身份 = **换档**，两个方向都可能——降成杂鱼是"他以后什么站内操作都做不了"，
    #     升回普通用户却是**把能力还给他**（同样的操作，读起来像做好事）。
    # 所以卡面必须把「从什么身份 → 什么身份」印全（`adminops.render_account_role`）：
    # 只看"把 guest5 改成杂鱼"这半句，主人会漏掉"他现在是普通用户"这件事，
    # 而这正是他判断"这一下是罚还是恢复"的全部依据。
    # 命令式措辞（「把 guest5 改成杂鱼」）在这件事上判得出来，但判得出来不等于该免问
    # ——与冻结族同一条理由：漏判的代价落在**别人**身上，且改错了要再改一次才能复原。
    "set_account_role",
    # 20261002 补**禁言 / 解除禁言**（内容风控下放给 agent）。进表的理由与冻结族
    # 同源——动的**不是主人的东西**：后果落在**第三方的发言能力**上（留言与评论是
    # 站内唯一对所有人可见的输出口，被他这么一说就发出去了，而且没有撤回通道）。
    # 命令式措辞（「把 guest5 禁言」「给他禁三天」）在这件事上判得出来，但**判得出来
    # 不等于该免问**（同冻结族那条：漏判的代价不是"多做一件小事"，是**堵住一个活人
    # 的嘴**，而且他本人只能从"评论发不出去"里察觉）。顺带一提，这也是"管理员之间
    # 不可互相禁言 / 超管谁都不能禁"那两条后端规则的**人眼复核点**：卡面把账号名、
    # 现状与期限印出来，主人点确定前能核对"是不是那个人、关多久"。
    # ⚠️ **后果句一个字节都不许复用冻结族**（禁言不踢人下线）：见 `adminops._MUTE_*`
    #    那段头注与被 `tests/test_account_mute.py` 钉住的那条断言。
    "account_mute", "account_unmute",
    # 20261019 补**文章评论的两件写**（复核 / 删除）。两件都进表，理由与留言族那两件
    # **逐条相同**（评论是**访客写下的内容**、不是主人的东西；复核可改判但主人本人
    # 认不出 commentId；删除软删、后台标「已删除」但公开侧立刻看不见）：
    #   · `audit_comment` 进表的理由与 `audit_board_comment` 那条一字不差——免问的前提
    #     是"这句话是主人下的命令"，而命令里**没有目标**（编号是系统印的），判据能核对
    #     的只剩"他是不是在让我复核"这半；
    #   · `delete_comment` 进表的理由与 `delete_board_comment` 那条一字不差——动的不是
    #     主人的东西，而且删了公开侧就没了（评论虽是软删，主人自己**没有**恢复入口）。
    # ⚠️ 卡面（`adminops_comment.comment_ref`）必须把「文章《标题》」一起印出来：
    #    评论与留言在卡面上长得几乎一样，少了文章那一截，主人核对的是"一条没有归属的
    #    评论"——那正是他点确定时唯一该看的东西。
    "audit_comment", "delete_comment",
    # 20261019 补**删标签**（安全审计 SAU-014，CWE-354）。它此前**不在**这张表，而它
    # 走同轮快道：用户原话含命令骨架（「把标签 X 去掉」）即直接执行，不经人眼。
    # 进表的理由与留言/评论族那两件**同源但更硬**——后果不可逆且**成片**：
    #   · 删**一级**标签会 CASCADE 连坐它下面**全部**二级标签（`tools/tag_admin.py:412`
    #     那段注自己写着"删一级时会连坐子标签"），并从**所有文章**上摘掉引用；
    #   · 站内没有"恢复标签"这个动作：删掉的标签与它挂过的文章之间的关联**重建不回来**
    #     （重建一个同名标签是一条新记录，文章不会自己挂回去）。
    # ⚠️ 同族的 `delete_category` **刻意不进**：后端外键是 `ON DELETE SET NULL`
    # （`tools/category_admin.py:237`），文章只是变成"没有分类"、不连坐、不摘引用
    # ⇒ 后果量级差一档。这两件在代码上长得几乎一样，所以把差异写在这里，免得下一个人
    # 按"看着一样"把它们一起加进来或一起拿出去。
    # 卡面必须印出**连坐范围**（`adminops_tags` 的删除回执已带子标签名单与受影响篇数），
    # 那是主人点确定前唯一读得到的后果句。
    "delete_tag",
})


def always_confirm_tools() -> frozenset[str]:
    """`_ALWAYS_CONFIRM_TOOLS` 的**公开读口**（20261014）。

    存在的理由只有一个：写参数出处闸的**强制域**要按"卡片会不会把值全量印出来"
    派生（见 `agent/provenance.py` 头注 1），而那个判据住在另一个模块里。**这里不
    新增任何语义**——表是同一份，函数名只是让"谁读它"能 grep 出来，而不是让下一个人
    去 import 一个下划线名（那正是"两份名单各漂各的"的开头）。
    """
    return _ALWAYS_CONFIRM_TOOLS


# 每个需确认的 scope 配一张**确认语表**：用户的**本轮消息**命中才算确认。
# 刻意收窄（"确认发布"这种明确说法）——fail-open 的代价是未经同意把内容发出去，
# 宁可多问一轮。扩表时先问一句：这句话会不会被误读成确认？
#
# 表值可以是 `re.Pattern`（`.search`）或**谓词 `(msg) -> bool`**（20260921 加）：
# write.console 的判据要同时管"有没有命令骨架"和"是不是在提问/假设"，写成一条
# 正则既读不懂也测不动——拆成几个具名小判据，在 `_console_command` 里组合。
_CONSENT_PATTERNS: dict[str, object] = {
    SCOPE_WRITE_CONSOLE: None,  # 占位，见下方 _console_command 注册
}


# ── write.console 的「命令式判据」（20260921；20261015 批 5 改按工具名判）────
# **它判的是"本轮有没有明确命令"，不是"第二次确认"**（用户拍板：同轮命令即确认
# ——管理员说「把《架构文档》设为私密」本身就是命令，再要一句"确认"是把确认闸
# 做成复读机）。三个小判据按 AND 组合，每一条都能单独写正反用例：
#
#   ① 有明确目标（id / 《书名号》/ 指代词 / 标签名）
#   ② 有**这个工具**的动作词（下方 `_CONSOLE_TOOL_ACTIONS` 的声明）
#   ③ 是命令句：有命令骨架（把/将/给，或动词起首），且**不是**疑问/假设/反问
#
# 提问与假设是这里最要命的误判来源：「把文章 12 设为私密会有什么影响？」若被判成
# 命令，闸就白设了。fail-closed 方向 = 判不出来就返回 False（planner 去追问），
# 代价是多问一轮，收益是绝不误写。
#
# **已知的刻意收窄**：「文章 12 设为私密」（无"把"的电报体）**不**判为命令——
# 它与陈述句「12 是私密的」在文本上无法可靠区分，而后者被误判成命令 = 用户只是
# 陈述现状、agent 却去写了。要放宽这条，先解决"陈述 vs 命令"的判别，别只删判据。
_CONSOLE_QUESTION_RE = re.compile(
    r"[？?]|吗|呢|怎么|为什么|为啥|是否|能否|能不能|可不可以|会不会|要不要|"
    r"有什么影响|有何影响|有没有|是不是|该不该|应不应该|好不好|行不行|对不对|"
    r"对吧|是吧|对吗|不是吗|怎么办|咋办|什么后果|风险")
_CONSOLE_HYPOTHESIS_RE = re.compile(
    r"^\s*(?:如果|假如|假设|要是|若是|万一|倘若|我想问|我想知道|想问|请问|问一下)")
# 命令骨架：句首的「把/将/给」，或「请/帮我…」起首——③「**句首动词**」那一支
# 20261015 批 5 从内联词表改成读声明（`_console_lead_order`，词表见
# `_CONSOLE_TOOL_ACTIONS` 的 `lead` 字段）。
_CONSOLE_ORDER_RE = re.compile(
    r"(?:^|[。！!；;\n，,])\s*(?:请|帮我|帮忙|麻烦|记得|快去)?\s*(?:把|将|给)"
    r"|^\s*(?:请|帮我|帮忙|麻烦|记得)\s*\S"
    # 「取消…置顶」把动词拆开了（宾语插在中间）："取消文章 12 的置顶"是**最常见的
    # 撤顶说法**，句首动词那一支的"取消置顶"连写认不到。只放 置顶/顶置 两个宾语——
    # 「取消…标签」那类不放：`功能/排序` 之类的抽象宾语会与闲聊撞车，而"取消标签"
    # 另有"把文章 12 的标签去掉/去掉标签"等**动词连写**的说法可走（fail-closed 的
    # 方向是多问一句，不是多写一次生产数据）。
    r"|^\s*(?:取消|解除)[^\n。！？!?；;，,]{0,12}?(?:置顶|顶置)")
# 疑问尾：出现即说明这是**在打听**（多久/多少钱/怎么弄），不是在下命令。
# 收窄的理由和上面一样——一个误判的代价是一次真写，而多问一次的代价只是弹个窗。
_CONSOLE_INQUIRY_TAIL_RE = re.compile(
    r"多少|多久|几天|费用|价格|流程|步骤|怎么|怎样|如何|什么样|能不能|可不可以|"
    r"需要|要不要|吗|呢")


# ── 每个后台写工具的「动作词」声明（20261015 批 5）──────────────────────
# 判据只回答"主人这句话里有没有**这个工具**会做的那个动作"。
#
# 上一版是一张 46 项的**共享**词表（`_CONSOLE_VERBS`，已删），三个后果：
#   ① 手抄维护，且 13 项是 `设为/设成/改成/转成/转为 × 私密/公开/草稿` 这种组合排列
#      ——判据用 `in` 子串匹配，裸词已在表里 ⇒ 组合项一个都不增加命中面；
#   ② 判据**工具无关**：任何后台写工具撞上表里任何一个词就判"这是命令"，于是
#      「把分类「随笔」改名叫「碎笔」」对 `delete_category` 也判成立（**误靶写**——
#      而 scope 级同意是全族共享的一张，兜不住工具错配）；
#   ③ 新加一个后台写工具**自动继承**整张表，不需要任何人想过它该认哪些词。
#
# 现在改成与 `_own_command` 同一体例：**逐工具声明**，判据按工具名取表。三条性质随之
# 成立——误靶关掉（目标写对了工具、又说了该工具的动作词，命令才成立）；完备性可锁
# （键必须**恰好**是"够得着快道的工具"，见 `console_fastpath_tools()`，新工具不声明就
# 红）；表能变小（组合项与子串冗余项全删，46 → 33 个字符串）。
#
# 三个字段对应三处**位置**，不是三种同义词：
#   · `body` = 句中动作词（`in` 子串）——"这句话里提到了这个动作"；
#   · `lead` = 能当**句首**动词的（`startswith`）——命令骨架第三支。**刻意**不进
#     `lead` 的那些词（`新增/建一个/建个/草稿/顶置/撤下`）在旧版同样是"句中算、句首
#     不算"（旧骨架支只有 8 个词）⇒ 这一支逐词照旧，不趁改造放宽；
#   · `echo` = 能跟在「确认…」后面的（`_console_confirm_order`）。**与 lead 不同源**：
#     echo 的选词标准是"主人照抄 agent 建议时会原样打出来"（「确认创建标签 X」），
#     lead 的标准是"能直接起首当祈使动词"——`设为/设成` 只满足前者，`建/加`（单字）
#     只满足前者，而 `新增/建一个` 两者都不满足。
#
# **留空也是声明**：`delete_*` 两件的 `lead`/`echo` 是空元组——删除族的连写说法
# （「把标签 X 删掉」）刻意**不判命令**、走弹窗，这是既有取向
# （`tests/test_admin_write.py` 的 `_unreachable` 逐条锁着它），本批只把"刻意"从
# 注释里搬进表里（原先它体现为"词表恰好没长这几个词"，谁来加一个词都看不出来）。
class _ConsoleWords(NamedTuple):
    """一个后台写工具认哪些说法算"它自己的动作"（字段语义见上方那段头注）。"""

    body: tuple[str, ...]
    lead: tuple[str, ...]
    echo: tuple[str, ...]


# 共享词族：同一件事在多个工具上说法相同，就在这里写一次、被各自引用（改一处即全改）。
# 每一项都取"动作本体"，不带组合（组合项是子串冗余，见头注①）。
#
# ⚠️ **组内顺序有意义**：下标切出来的那几支（下面的 `*_LEAD` / `*_ECHO`）是本族里
# "还能用在别的位置"的子集，靠切片表达而不是再抄一遍字面量——`eval/wordlist_gate.py`
# 的穷举量数的是**字面量出现次数**，同一个词写两处就算两份（旧版词表里 13 项组合词
# 正是这么长出来的）。改这几组的顺序时，跟着看一眼下方切片的注释。
_CONSOLE_STATUS_WORDS = ("私密", "公开", "隐藏", "发布", "下架", "草稿", "撤下")
_CONSOLE_TOP_WORDS = ("置顶", "取消置顶", "取消顶置", "顶置")
# 「改成/改为/换成」与「改名」分开用：前者是**重新指派**的说法（「把文章 12 的标签改成
# Python 和 架构」= 清空重挂，`set_article_tags` 也认），后者只针对**实体本身的名字**
# （`update_tag`/`update_category`）⇒ 前三个切出来给两个消费方，第四个只在改名族里。
_CONSOLE_RENAME_WORDS = ("改成", "改为", "换成", "改名")
_CONSOLE_CREATE_WORDS = ("新建", "创建", "建立", "新增", "建一个", "建个", "添加")
_CONSOLE_TAG_MARK_WORDS = ("打上", "加上", "打个")
_CONSOLE_DROP_WORDS = ("去掉", "移除")
# （原 `_CONSOLE_TAG_DROP_WORDS = ("取消标签", "删掉标签")` 随 `delete_tag` 一起摘掉：
#   20261019 它进了「一律弹窗」族 ⇒ 不再走快道 ⇒ 这组词没有任何消费方了。
#   留着它比删掉更坏：下一个人会以为"标签删除还有一条快道"，而那条路已经不存在。）

# 句首动词（命令骨架第三支）：旧版那张内联表逐词照搬——
#   状态 = 私密/公开/隐藏/发布/下架（**不含** 草稿/撤下）；置顶族 = 置顶/取消置顶/取消顶置
#   （**不含** 顶置）；创建族 = 新建/创建/建立/添加（**不含** 新增/建一个/建个）
_CONSOLE_STATUS_LEAD = _CONSOLE_STATUS_WORDS[:5]
_CONSOLE_TOP_LEAD = _CONSOLE_TOP_WORDS[:3]
_CONSOLE_CREATE_LEAD = _CONSOLE_CREATE_WORDS[:3] + _CONSOLE_CREATE_WORDS[6:7]

# 重新指派（不改实体名）：`set_article_tags` 用它（旧版整张共享词表里这三个词对它同样
# 成立 —— 「把文章 12 的标签改成 Python」是它的教科书说法）。
_CONSOLE_SET_WORDS = _CONSOLE_RENAME_WORDS[:3]

# 「确认…」回声（旧版第二条内联表逐词照搬）：状态 = 设为/设成/改成/置顶/取消置顶/
# 隐藏/发布/下架；标签族 = 打上/加上/去掉/移除；创建族 = 创建/新建/建立/新增/建/加。
# 只有「设为/设成」与「建/加」是本族里没有的新词——它们在旧版也只出现在回声那一支。
_CONSOLE_STATUS_ECHO = (("设为", "设成") + _CONSOLE_RENAME_WORDS[:1]
                        + _CONSOLE_TOP_WORDS[:2] + _CONSOLE_STATUS_WORDS[2:5])
_CONSOLE_CREATE_ECHO = _CONSOLE_CREATE_WORDS[:4] + ("建", "加")
_CONSOLE_RENAME_ECHO = _CONSOLE_RENAME_WORDS[:1]
_CONSOLE_TAG_MARK_ECHO = _CONSOLE_TAG_MARK_WORDS[:2]

# 键集 = `console_fastpath_tools()`，由 `tests/test_authz.py` 双向锁（漏一个红、
# 多一个红）。**13 件一律弹窗的工具不在表里**：它们走不到"同轮命令即确认"这条快道
# （`consent_granted` 对它们恒 False），给它们声明动作词等于声明一条不存在的路径。
_CONSOLE_TOOL_ACTIONS: dict[str, _ConsoleWords] = {
    # 改文章的发布状态与置顶（`set_article_status`，唯一带 is_top 的工具）
    "set_article_status": _ConsoleWords(
        body=_CONSOLE_STATUS_WORDS + _CONSOLE_TOP_WORDS,
        lead=_CONSOLE_STATUS_LEAD + _CONSOLE_TOP_LEAD,
        echo=_CONSOLE_STATUS_ECHO,
    ),
    # 给文章打/清标签（`set_article_tags`）：打上/加上/打个、去掉/移除、以及
    # 「把…的标签**改成** X」（重新指派那一族，见 `_CONSOLE_SET_WORDS`）——不含「改名」
    # （那是改标签实体本身的名字，`update_tag` 的事）。
    "set_article_tags": _ConsoleWords(
        body=_CONSOLE_TAG_MARK_WORDS + _CONSOLE_DROP_WORDS + _CONSOLE_SET_WORDS,
        lead=(),
        echo=_CONSOLE_TAG_MARK_ECHO + _CONSOLE_DROP_WORDS + _CONSOLE_RENAME_ECHO,
    ),
    # 新建标签（`create_tag`）
    "create_tag": _ConsoleWords(
        body=_CONSOLE_CREATE_WORDS + _CONSOLE_TAG_MARK_WORDS,
        lead=_CONSOLE_CREATE_LEAD,
        echo=_CONSOLE_CREATE_ECHO + _CONSOLE_TAG_MARK_ECHO,
    ),
    # 改标签（`update_tag`）：只认"改名/改状态类"，**不含**去掉/移除那一族——
    # 那些是 `set_article_tags`（改文章身上的标签）与 `delete_tag`（删标签实体）的
    # 说法，撞上就是误靶写（今天「把标签 X 去掉」对 update_tag 判成立）。
    "update_tag": _ConsoleWords(
        body=_CONSOLE_RENAME_WORDS,
        lead=(),
        echo=_CONSOLE_RENAME_ECHO,
    ),
    # ⚠️ **`delete_tag` 刻意不在这里**（20261019 安全审计 SAU-014）：它进了
    # `_ALWAYS_CONFIRM_TOOLS` ⇒ 每轮都弹卡 ⇒ 快道这条路**走不到了**。
    # `tests/test_authz.py` 有一条锁判的正是这件事：「给一律弹窗的工具声明动作词
    # = 声明一条走不到的路径」。所以下面那三个消费方也一并摘掉：
    # `_CONSOLE_TAG_DROP_WORDS` 常量、技能描述里的两档措辞、`_console_target` 的负词依赖。
    # 新建分类（`create_category`）
    "create_category": _ConsoleWords(
        body=_CONSOLE_CREATE_WORDS,
        lead=_CONSOLE_CREATE_LEAD,
        echo=_CONSOLE_CREATE_ECHO,
    ),
    # 改分类（`update_category`）
    "update_category": _ConsoleWords(
        body=_CONSOLE_RENAME_WORDS,
        lead=(),
        echo=_CONSOLE_RENAME_ECHO,
    ),
    # 删分类（`delete_category`）：只留"去掉/移除"这一族（「把分类 X 去掉」），
    # 与 `delete_tag` 一样**不认**句首动词与确认回声。
    "delete_category": _ConsoleWords(body=_CONSOLE_DROP_WORDS, lead=(), echo=()),
}


def console_fastpath_tools() -> frozenset[str]:
    """**够得着"同轮命令即确认"快道**的后台写工具（= write.console 全体 − 一律弹窗那批）。

    这是 `_CONSOLE_TOOL_ACTIONS` 键集的**真值源**（不是手抄的第二份名单）：新增一件
    后台写工具时，它要么进 `_ALWAYS_CONFIRM_TOOLS`（每次弹卡），要么必须有人为它写一行
    「它认哪些动作词」的声明——不写就红（`tests/test_authz.py` 的完备性锁）。旧版没有
    这条：新工具**静默继承**那张 46 项共享词表，于是"这个工具该不至于被一句话直接写
    生产数据"这件事从来没人确认过。
    """
    return frozenset(
        t for t, s in TOOL_SCOPE.items() if s == SCOPE_WRITE_CONSOLE
    ) - _ALWAYS_CONFIRM_TOOLS


# 「确认…」回声的骨架（**只认句首**）：整句必须以它开头，后面接什么由
# `_console_confirm_order` 按工具声明判。词表拆走、骨架留下——骨架是固定格式的解析。
_CONSOLE_CONFIRM_ECHO_RE = re.compile(r"^\s*确认(?:一下)?\s*")

# 「标签 / 叫 <名字>」形态的目标：名字取到空白或标点为止。
_CONSOLE_TAG_NAME_RE = re.compile(
    r"(?:叫|名为|叫做|名字是|名称是|标签)\s*[：:]?\s*[\"'“”‘’]?"
    r"([^\s，。！？!?；;\"'“”]{1,30})")


def _console_target(text: str, acts: "_ConsoleWords") -> bool:
    """消息里有没有**明确的目标**（数字 id / 《书名号》/ 指代词 / 一个标签名）。

    标签名那一支要做一次反查：「把**标签去掉**」里"标签"后面跟的是**动作词**，
    不是名字——只看正则的话它和「新建标签 Python」长得一模一样，会把
    「把标签去掉」判成有目标的命令。所以命中后要求那个"名字"不落在**这个工具**的
    动作词里（`acts.body`；旧版读的是那张共享表，于是「把标签 X 删掉」对任何工具
    都算"没有目标"——工具无关的副作用：`update_tag` 拿到的是别的工具的词）。
    """
    if re.search(r"\d+", text):
        return True
    if "《" in text and "》" in text:
        return True
    if re.search(r"这篇|这篇文章|当前文章|此文|该文", text):
        return True
    m = _CONSOLE_TAG_NAME_RE.search(text)
    if not m:
        return False
    name = m.group(1)
    return not any(v.startswith(name) or name.startswith(v) for v in acts.body)


def _console_lead_order(text: str, acts: "_ConsoleWords") -> bool:
    """命令骨架第三支：**句首**就是这个工具的动作词（「发布文章 12」「新建一个分类」）。

    与句中那支分开判：句中那支是 `in` 子串（说了这个动作就算），句首这支锚定
    （`startswith`）。两支队列表也分开声明——"能不能在句首当动词"是每个工具自己的事，
    旧版把两支写在同一张表里，加词时无从知道加的是哪一个位置。
    """
    head = text.lstrip()
    return any(head.startswith(w) for w in acts.lead)


def _console_confirm_order(text: str, acts: "_ConsoleWords") -> bool:
    """「确认 + **这个工具的**动作词」的**短回声**骨架（见 `_CONSOLE_CONFIRM_ECHO_RE`）。

    两支的区分留在**结构**里：① 把字结构（「确认把文章 12 设为私密」——动词在宾语
    之后）；② 动作词直接跟在确认之后（「确认创建标签 X」）。词表各读工具的 `echo`
    声明——旧版这两支各带一张内联词表，与 `body` 那张表必须人工同步（同族缺陷）。
    """
    m = _CONSOLE_CONFIRM_ECHO_RE.match(text)
    if not m:
        return False
    if _CONSOLE_INQUIRY_TAIL_RE.search(text):
        return False
    rest = text[m.end():]
    if rest.startswith(("把", "将", "给")):
        # ① 确认 + 把/将/给 + 宾语 + 动作词。宾语窗口与旧正则逐字一致（1–20 字、
        #    **不跨句读**）——收窄它没必要，放宽它则是一处没人量过的行为变化。
        gap = re.match(r"[^\n。！？!?；;，,]{1,20}", rest[1:])
        if gap is None:          # 显式判空：`bool(gap) and …` 是同一件事，
            return False         # 但 mypy 穿不过布尔运算去做收窄（棘轮会红一条）
        return any(w in rest[1:1 + gap.end()] for w in acts.echo)
    return any(rest.startswith(w) for w in acts.echo)  # ② 确认 + 动作词


# 系统注入的消息壳（`server.py` 的 `_build_messages` 给本轮用户消息加的
# `[当前问题]: ` 锚点）。
#
# 这是**系统加的外壳，不是用户的话**，而底下所有判据都是锚定的（句首把/将、句首
# 动词、句首假设词）：带着壳一条都命不中。生产实测（20260921）——
# 「[当前问题]: 把文章 999999 设为私密」这种**教科书式的明确命令**在同意闸里
# 判的是 False（弹窗照弹），而「[当前问题]: 如果我把文章 12 设为私密」这种假设
# 也判不出提问（弹窗照弹，把假设读成了意图）。两处根因同一个：判据看到的是
# 包装过的文本。所以判据入口统一先剥壳。
#
# 剥掉的只是方括号注记（`[…]`，最多两层语义、32 字以内），剥完仍要过目标/动作/
# 骨架三关——用户自己写「[求助] 把文章 12 设为私密」剥完照样是命令，他本来也
# 就是在下命令，不构成放宽。
_SYS_TAG_RE = re.compile(r"^(?:\s*\[[^\[\]\n]{0,32}\]\s*[:：]?\s*)+")


def _strip_system_tags(text: str) -> str:
    """剥掉消息开头的系统方括号注记（见 _SYS_TAG_RE）。"""
    return _SYS_TAG_RE.sub("", text or "", count=1)


# 公开别名（20260923）：**确定性快道**的判定入口同样要先剥壳（`decisions._bare`）——
# 导航快道的两条入口是句首锚定，被同一个壳挡住，自 20260901 起在生产里从未命中过。
# 共用同一个"用户实际说了什么"的定义，避免两处各写一套剥法再各自漂移。
strip_system_tags = _strip_system_tags


# ── 句首**称呼壳**（20260930，同族第 4 例）────────────────────────────────────
# 第二层壳，**不是系统加的、是主人的说话习惯**：人设让它叫「小猫咪/泠月喵」，主人就
# 真的这么叫——生产语料 1110 条消息里 **252 条（22.7%）** 以称呼开头。
#
# 它架空判据的机制与系统壳一模一样：底下全是句首锚定（句首把/将、句首动词）或整串相等
# 型，称呼一在就恒不命中。实测（同一份语料 1110 条，剥与不剥逐条对账）：
#
#   · 同意闸 `_own_command` **6 条翻正**——5 条是「小猫咪把这篇文章收藏了」/「小猫咪把
#     我当前在读的文章收藏了」（这 5 轮 trace 里主人随后都点了「确定」把收藏办成了
#     ⇒ 此前是判据判不出、弹窗问一次），1 条是「猫咪我的未读信息全部就标记为已读」
#     （洞⑨ 那句事故原文）。
#   · 弹窗分叉 `is_question_like` 8 条翻正——全是「小猫咪！」「小猫咪」这种**只叫名字**
#     的轮次：剥完为空 ⇒ 按"无从判断时不弹"走，方向与全表一致（叫名字不是意图）。
#   · 后台写判据 `_console_command`、短应答判据（20261015 批 3 已整族删，改纯结构准入）、
#     导航快道：**0 条变化**
#     （导航快道的老表已内联认「小猫咪」，本判据多认的是裸「猫咪」——语料里还没有这种
#     说法，属可预期的加宽；「猫咪带我去留言板」实测命中）。
#
# 剥壳**不放宽判据**：剥完仍要过 提问/假设/打听/陈述/骨架 五关——主人写「小猫咪，把文章
# 12 设为私密」本来就是在下命令，不构成放宽；而「小猫咪，如果我把文章 12 设为私密呢」
# 剥完照样被假设关拦下。
#
# ⚠️ **称呼表只有这一份**：`decisions._NAV_VERB_RE` 与 `context._SHORT_LEAD_RE` 里各内联过
# 一份（都不含裸「猫咪」，于是同一句话在两处口径不同）——20260930 一并收进来，全部改走
# `strip_user_shell`。再加称呼只改这里。（`_SHORT_LEAD_RE` 本身 20261015 批 3 已删，
# 位置被 `prompt_context._short_core` 接走；`decisions` 那一处照旧。）
_VOCATIVE_RE = re.compile(
    r"^\s*(?:(?:小|大|傻|臭|笨|乖)?(?:猫咪|猫猫)|泠月喵|泠月|喵喵|喵|主人)"
    r"(?:\s*[，,、:：!！~～。.…]+\s*|\s+)?")


def _strip_address_shell(text: str) -> str:
    """剥掉消息开头的称呼壳（见 `_VOCATIVE_RE`）。只剥**开头**一处，句中不动。"""
    return _VOCATIVE_RE.sub("", text or "", count=1)


def strip_user_shell(text: str | None) -> str:
    """判据入口的**唯一**剥壳定义：系统锚点壳 + 句首称呼壳 → **用户实际说的那句话**。

    只用于**判定**，不碰给模型的 prompt（两层壳都是给模型看的语用信息，不是缺陷）。
    """
    return _strip_address_shell(_strip_system_tags((text or "").strip()))


def _console_command(msg: str, tool: str | None = None) -> bool:
    """本轮消息是不是一条明确的**后台写**命令（确定性、无 LLM）。

    `tool` 决定"哪些动作词算数"（20261015 批 5，见 `_CONSOLE_TOOL_ACTIONS`）——**不知道
    是哪个工具就判不了**：判据问的是"这句话是不是**这个工具的**命令"，少了工具那一半
    等于回到那张共享词表（任何工具撞上任何动作词都算命令）。所以 `tool=None` 或工具没
    声明 ⇒ **False**（fail-closed，走弹窗那条路，与 `_own_command` 同一取向）。

    旧签名收下 `tool` 而不用它，理由是"落到哪个工具由技能模板与 `_ARTICLE_WRITE_TOOLS`
    的目标校验管"。那句话今天仍然成立（**写什么**由它们管），但它答的不是本判据的问题
    ——本判据要回答的是「主人这句话是不是在命令**这一件**事」，而"目标校验"只在工具已经
    被选中之后运行，管不到"选错了工具"。
    """
    acts = _CONSOLE_TOOL_ACTIONS.get(tool or "")
    if acts is None:
        return False
    text = strip_user_shell(msg)          # 系统壳 + 称呼壳：判据看到的是主人实际说的那句
    if not text:
        return False
    if _CONSOLE_QUESTION_RE.search(text) or _CONSOLE_HYPOTHESIS_RE.search(text):
        return False
    if not _console_target(text, acts):
        return False
    if not any(v in text for v in acts.body):
        return False
    return (bool(_CONSOLE_ORDER_RE.search(text))
            or _console_lead_order(text, acts) or _console_confirm_order(text, acts))


def is_question_like(msg: str) -> bool:
    """这句是**提问/假设**（而不是一个意图陈述）吗？

    弹窗分叉用它（graph.py::execute_node）：判不出来是"用户有意向但没判成命令"
    → 弹窗问一次；判出来是提问/假设 → 绝不能弹（用户只是在问，弹一个"确定/取消"
    等于把提问读成了意图）。空消息保守按提问走（无从判断时不弹）。
    """
    text = strip_user_shell(msg)          # 系统壳 + 称呼壳：判据看到的是主人实际说的那句
    if not text:
        return True
    return bool(_CONSOLE_QUESTION_RE.search(text) or _CONSOLE_HYPOTHESIS_RE.search(text)
                or _CONSOLE_INQUIRY_RE.search(text))


# 打听类名词/句式（**只给弹窗分叉用**，见 is_question_like）
#
# 上一张疑问词表是给**同意闸**用的（"这句是不是一条命令"），它漏掉了一类很常见的
# 问法：「文章 12 设为私密的**步骤是什么**」——没有 吗/呢/怎么，也没有假设前缀，
# 在同意闸那边的后果只是"不算命令"（去追问，无妨）；但在弹窗分叉那边，这句话会
# **弹出一个"确定/取消"框**，把纯提问读成了意图（用户拍板明确不许）。
#
# 20260925 句式化（生产事故 trace 20260924T234402）：这一版之前表里躺着的是**裸名词**
# 词表（步骤|流程|条件|要求|影响|…），于是"要求/注意"这类词只要字面上出现就算提问。
# 现场：「小猫咪替我发一个公告，**要求**全体用户今晚不许熬夜」→ 判成提问 → 弹窗分叉
# 直接 `return None`（graph.py::_confirm_popup 第一道闸）→ 公告是 `_ALWAYS_CONFIRM_TOOLS`
# 成员、同意闸恒不放行 ⇒ **一份公告都没有执行途径**，planner 连着四轮改写正文，其中一轮
# 开始替访客编造"主人说……"去够同意闸（trace 里那句薛定谔的猫是这么来的）。
#
# 修法：疑问锚必须落在**句法位置**上——① 疑问式本身（是什么/有多少/…吗）；② `X的<名词>`
# （「设为私密的**步骤**」）；③ `什么<名词>`（「走**什么流程**」）。裸名词一律不判提问：
# 它是内容里的普通词（"公告**要求**全体用户…"里它就是这个意思）。
#
# 反面同样重要（这条表是"多判一个提问 = 少弹一张卡 = 可能一条写操作再也没有执行途径"）：
# 宽词"什么"仍然不许单独进表——「新建个标签，名字叫什么好」是真意图，判成提问就又回到死路。
_INQUIRY_NOUNS = r"步骤|流程|条件|要求|影响|后果|风险|注意|区别|好处|坏处"
_CONSOLE_INQUIRY_RE = re.compile(
    r"是什么|是啥|有什么|有多少|多少|多久|几天|可以吗|行吗"
    r"|的(?:" + _INQUIRY_NOUNS + r")"
    r"|(?:什么|哪些)(?:" + _INQUIRY_NOUNS + r")")

# 上一版那张**裸名词**词表，只剩 `_own_command` 在用（见那里对方向的取舍说明）：
# 它判的是"这句是不是一条写自己数据的命令"，判错的方向是**多一次点击**，与弹窗分叉
# 判错的方向（少一张卡）正好相反，所以它保留宽表——句式化只动弹窗分叉那一支。
# 两张表分开写而不是叠一层开关：它们服务于两个方向相反的判据，改动本来就不该互相绑架。
_CONSOLE_INQUIRY_BROAD_RE = re.compile(
    r"是什么|是啥|有什么|有多少|多少|多久|几天|" + _INQUIRY_NOUNS +
    r"|可以吗|行吗")


_CONSENT_PATTERNS[SCOPE_WRITE_CONSOLE] = _console_command


# ── write.own 的「命令式判据」（20260923）───────────────────────────────
# 与 write.console 同源（判"本轮有没有明确命令"，不是第二次确认），但**比它严一档**：
# 这里的谓词额外拿到**工具名**（`consent_granted` 对可调用值传第二个参数），于是判据
# 回答的是"用户这句话是不是在命令**这一个**工具"，而不是"是不是在命令这一族"。
#
# 为什么要多这一道：一个 scope 下现在有三个工具（收藏/取消收藏/标记已读），而同意闸
# 是**按 scope** 查表的。若只看"这句里有没有写动作"，那么用户说「把通知都标记已读」
# 时 planner 若填错成 `add_favorite`，scope 级同意照样放行——**误靶写**（20260921
# golden 实测过的那一类：planner 拿列表首行当用户点名的那一篇）会在一个本来是为了
# 防它的闸门里溜过去。工具名进来之后，"这句话里有没有**这个动作**"就是判据本身。
#
# 家族名：`_OWN_TOOL_FAMILY` 是工具 → 家族的**完备映射**——没登记的工具一律 False
# （fail-closed，绝不用"反正都是 own 就放行"兜底），tests/test_authz.py ⑨d 锁完备性。
_OWN_FAMILY_ADD = "add"
_OWN_FAMILY_REMOVE = "remove"
_OWN_FAMILY_READ = "read"

_OWN_TOOL_FAMILY: dict[str, str] = {
    "add_favorite": _OWN_FAMILY_ADD,
    "remove_favorite": _OWN_FAMILY_REMOVE,
    "read_notifications": _OWN_FAMILY_READ,
    # 标记信已读与标记通知已读是**同一个动作家族**（"把 X 标记已读"），共用
    # `_OWN_READ_RE`：它只认"标记/标注…已读"这种连用，与随口说一句"标记"不撞车。
    "read_messages": _OWN_FAMILY_READ,
}

# 命令骨架（与后台写同构：句首的「把/将」，或动词起首，前面允许礼貌前缀）：
# 只认这两支是**刻意的**——「文章 12 我收藏了」这种**陈述句**（无骨架）判不出来，
# 方向是 fail-closed（多问一句），因为把陈述读成命令＝用户只是描述现状、agent 却去写。
_OWN_POLITE = r"(?:\s*(?:请|帮我|帮忙|麻烦|记得|给我|替我|帮我一下)\s*)?"
# 陈述句排除：句首「我/咱」+ 可选副词 + 「把/将」——「我已经把这篇文章收藏了」是
# 在告诉我状态，不是在命令我。疑问/假设由下面两张共享正则管。
_OWN_STATEMENT_RE = re.compile(r"^\s*(?:我|咱|俺)\s*(?:已经|刚|刚刚|刚才|早就|之前|先前)?\s*(?:把|将)")

# 「收藏」两个字是**减法说法的子串**（"取消**收藏**"/"从**收藏**里去掉"/"不**收藏**了"）：
# 把字结构只看"把…收藏"的话，一句明确的**取消收藏**命令会被判成"收藏命令"，然后被下面的
# 家族冲突规则拦掉（→ 弹窗）——那是把最常见的撤收藏说法当成判不准。所以在"把"之后先扫
# 一眼这段宾语里有没有撤除词，有就不是 add 族。
_OWN_REMOVE_MARK = r"(?:取消|撤[销掉]|移除|移出|删掉|删除|去掉|不再|不想|别|不|从|收藏[里中])"
_OWN_ADD_RE = re.compile(
    # ① 把字结构：宾语在动词之前（"把文章 12 收藏一下"/"把那篇加入收藏"）
    rf"^(?:{_OWN_POLITE})?(?:把|将)(?![^\n。！？!?；;，,]{{0,24}}?{_OWN_REMOVE_MARK})"
    r"[^\n。！？!?；;，,]{1,24}?(?:加入收藏|加到收藏|放进收藏|收进收藏|收藏)"
    # ② 动词起首（"收藏这篇"/"帮我收藏《架构文档》"）——**只认句首紧跟的那个动词**：
    #    「帮我看看收藏」里的"收藏"是名词（页面名），拿它当动词就会凭空多写一条收藏。
    rf"|^(?:{_OWN_POLITE})?(?:加入收藏|加到收藏|放进收藏|收进收藏|收藏)"
    r"(?=$|[\s一下起来这那篇文章「《\d])"
)
_OWN_REMOVE_RE = re.compile(
    rf"^(?:{_OWN_POLITE})?(?:把|将)[^\n。！？!?；;，,]{{1,24}}?"
    r"(?:取消收藏|不再收藏|移除收藏|删掉收藏|删除收藏|移出收藏|去掉收藏"
    r"|从收藏[里中]?[^\n。！？!?；;，,]{0,4}?(?:去掉|移除|删掉|删了|移出|取消))"
    # 「把**收藏里**的文章 12 删掉/去掉」——宾语是"收藏里的那一篇"，撤除动词跟在文章
    # 后面。**单独一支**：上面那支的宾语至少 1 字（`{1,24}`），而这里"收藏"紧贴"把"
    # （把＋收藏里…），挤不进那个 span。同样只认撤除动词，不认裸"删"。
    rf"|^(?:{_OWN_POLITE})?(?:把|将)收藏[里中][^\n。！？!?；;，,]{{0,16}}?"
    r"(?:删掉|删除|去掉|移除|移出|取消)"
    rf"|^(?:{_OWN_POLITE})?"
    r"(?:取消收藏|不再收藏|不收藏了|移除收藏|删掉收藏|删除收藏|移出收藏|去掉收藏)"
    r"(?=$|[一下这那篇文章「《\d])"
)
# 已读族：动词**必须与「已读」连用**才成立——「把通知标记一下」不是标记已读，
# 而"通知"这个词本身到处都是，只认"标记"会与闲聊撞车。
_OWN_READ_VERB = r"(?:标记|标注|标为|标成|设为|设置|置为|改为|改成|转为)"
# 动词起首分支的两个**槽**（20260930，词形族加宽而不是逐个补写法）：
#   · 名词槽 = 物件的各种叫法。批 8（20260923）已证"叫法收不全 ⇒ 最自然的说法漏判"；
#     这一版补上「未读信息/未读消息/未读数/信息」——生产原文「猫咪我的未读信息全部就
#     标记为已读」正是栽在"未读信息"不在表里（而且它那轮 planner 判了 chat、零工具，
#     见 `_WRITE_DONE_CLAIM_RE` 的洞⑨）。
#   · 副词槽 = 动词与物件之间的口语插入（就/也/再/先/都/给我/帮我）。原判据只认"都"。
# 加宽面**逐条对过全量语料**（1110 条生产消息）：只有这一句话从 False 翻 True（两个读族
# 工具各算一次，落点在同一条消息上）——没有一个提问/陈述被读成命令。
_OWN_READ_NOUN = (r"(?:所有|全部|全|都|未读的?|未读通知|未读信息|未读消息|未读数"
                  r"|通知|公告|站内信|私信|信件|消息|信息)")
_OWN_READ_ADV = r"(?:就|也|再|先|都|给我|帮我)"
_OWN_READ_RE = re.compile(
    rf"^(?:{_OWN_POLITE})?(?:把|将)[^\n。！？!?；;，,]{{0,24}}?{_OWN_READ_VERB}"
    r"[^\n。！？!?；;，,]{0,6}?已读"
    # 动词起首（"标记已读"/"全部标记为已读"）：前面允许挂几个量词/名词（全部/未读的/通知）
    # 名词表覆盖两种物件的两种叫法（通知族：通知/公告；信族：站内信/私信/信件/消息）
    # ——物件本身不参与判据（同一个动作家族，见 _OWN_TOOL_FAMILY 的注释），但**叫法
    # 要收全**：少了「私信」的话，「我的私信都标成已读」这句最自然的说法会漏判、
    # 落回弹窗（20260923 批 8 实测：只有「把私信…」那种把字结构能过）。
    # 「我(的)」这个所有格前缀也认（「我的私信都标成已读」是最自然的一种说法；
    # 只取所有格，不含「已经/刚」那类时间副词——带时间副词的句子是**陈述现状**，
    # 由上面的 _OWN_STATEMENT_RE 管，别在这里放进来）。
    rf"|^(?:{_OWN_POLITE})?(?:(?:我|咱|俺)(?:的)?\s*)?"
    # ⚠️ 这一行必须是 **rf** 串：`{{0,4}}` 在 rf 里才折叠成量词 `{0,4}`，写成普通 r 串
    # 就变成"字面量 {0,4}"（永远匹配不上）⇒ 整支动词起首分支恒不命中（本批改这句时
    # 真的发生过一次：只有「把…」那支还能过）。
    rf"(?:{_OWN_READ_NOUN}\s*){{0,4}}"
    # 动词前的**副词槽**（20260930）：口语里"未读信息**全部就**标记已读""**也**标成已读"
    # 极常见，而原判据只放"都"一个 ⇒ 一句明确的命令落回弹窗。这一槽**不碰骨架**：
    # 后面仍必须紧跟 `_OWN_READ_VERB` + 六字内的"已读"，陈述句照样进不来。
    rf"(?:{_OWN_READ_ADV}\s*){{0,2}}"
    + _OWN_READ_VERB + r"[^\n。！？!?；;，,]{0,6}?已读"
)

_OWN_FAMILY_RE: dict[str, re.Pattern] = {
    _OWN_FAMILY_ADD: _OWN_ADD_RE,
    _OWN_FAMILY_REMOVE: _OWN_REMOVE_RE,
    _OWN_FAMILY_READ: _OWN_READ_RE,
}

# 量词型打听（「我收藏了**哪些**文章」「还有**几条**没读」）。上面两张共享表都不含
# "哪些/几个"这类**问数量的词**（它们是给后台写用的，那里的问题词都由 吗/怎么 兜住）。
_OWN_INQUIRY_RE = re.compile(r"哪些|哪个|哪篇|哪条|几篇|几条|几件|几个|几次")


def _own_command(msg: str | None, tool: str | None = None) -> bool:
    """本轮消息是不是一条明确的「写自己私有数据」命令（确定性、无 LLM）。

    `tool` 是**必需**的（见上面那段注释：判的是"命令**这一个**工具"）。未登记家族的
    工具 → False（fail-closed）。
    """
    family = _OWN_TOOL_FAMILY.get(tool or "")
    if family is None:
        return False
    text = strip_user_shell(msg)          # 系统壳 + 称呼壳：判据看到的是主人实际说的那句
    if not text:
        return False
    if _CONSOLE_QUESTION_RE.search(text) or _CONSOLE_HYPOTHESIS_RE.search(text):
        return False
    # 打听类（「收藏文章有什么用」「取消收藏的**步骤**是什么」）：这里刻意用**宽表**
    # `_CONSOLE_INQUIRY_BROAD_RE`（裸名词也判提问），代价是"注意/要求"这类词也会让一句
    # 真命令落回弹窗、多一次点击——方向的取舍与全表一致：多一次点击 < 多一次误写。
    # 20260925：表已与弹窗分叉那支（句式化后）**分开**，两支判据的错向相反，不该互相绑架。
    if _CONSOLE_INQUIRY_BROAD_RE.search(text) or _OWN_INQUIRY_RE.search(text):
        return False
    if _OWN_STATEMENT_RE.search(text):
        return False
    if not _OWN_FAMILY_RE[family].search(text):
        return False
    # 同一句里还出现了**另一族**的动作（"取消收藏这篇，收藏那篇"）：这句到底要加还是
    # 要撤说不准 —— 判不准就交给弹窗问一次（fail-closed 的方向与全表一致）。
    for other, rx in _OWN_FAMILY_RE.items():
        if other != family and rx.search(text):
            return False
    return True


_CONSENT_PATTERNS[SCOPE_WRITE_OWN] = _own_command


# ── write.own 的**动作冲突**：反极性改选（20261016，§1.57 候选改法①）────────
# 病（docs/问题记录.md §1.57，20261009 记下、20261016 治）：`favorite_add` 被环境型
# 受阻（`unavailable`，uid=0 哨兵）判 BLOCK ⇒ `denied_workflows` 把**这一件**从菜单摘掉
# ⇒ 下一轮模型**改选**，选中的却是 `favorite_remove`（反极性兄弟）——菜单里只剩它
# 这一件"和收藏有关"的可点项。`remove_favorite` 不是主人那句话的命令
# （`_own_command` 判淡）⇒ 走正常弹卡 ⇒ 主人看到的是「**取消收藏**文章 12」，
# 而他刚说的是**收藏**。
#
# 判据的落点：§1.57 已经说清"不能按同族一起摘"——合法改选（`account_roster` 读被摘
# 之后改选 `unmute_account` 写）与反极性改选**结构上长得一样**。判据只能落在
# 「**主人这句话要的动作**」上，而那个量 `_own_command` 已经算得出来：同族＝放行、
# 反极性＝冲突。所以这一格不是新判据，是**同一个谓词的第二个用法**。
#
# 反极性映射由家族表**派生**，不手抄一份工具名表（手抄的那份会漂，本仓为它付过
# 代价）。ADD↔REMOVE 是同一件物件（收藏夹）上的**相反**动作；READ 族
# （通知/站内信）**没有**反极性——那两个工具是同一动作的不同物件，§1.57 里它们
# 属"改选"但不属"反极性"，这一格刻意不碰它们（"标记通知已读"被摘之后改选
# "标记站内信已读"是**另一件东西**，不是同一句话的反面）。
_OWN_ANTIPODE_FAMILY = {_OWN_FAMILY_ADD: _OWN_FAMILY_REMOVE,
                        _OWN_FAMILY_REMOVE: _OWN_FAMILY_ADD}
_OWN_ANTIPODE: dict[str, str] = {}
for _t, _f in _OWN_TOOL_FAMILY.items():
    _mates = [t for t, f in _OWN_TOOL_FAMILY.items()
              if f == _OWN_ANTIPODE_FAMILY.get(_f)]
    # 反极性家族**恰好一件**才算得出兄弟；多于一件 = 映射有歧义 ⇒ 不登记（这一格
    # 退化成不启用），由 `tests/test_own_action_conflict.py` 的台账锁把它钉住——
    # 家族表长出新成员时那条锁当场红，不会静默少拦一格。
    if len(_mates) == 1:
        _OWN_ANTIPODE[_t] = _mates[0]


def own_antipode(tool: str | None) -> str | None:
    """同一件物件上**相反**动作的工具名（无 → None）。台账锁见测试套件。"""
    return _OWN_ANTIPODE.get(tool or "")


# 家族 → 代表工具（逐子句扫"这句说了哪几族"用）。同族共用一个判据正则，取哪个都等价
# （`_OWN_FAMILY_RE` 是按族建的，不是按工具）——READ 族两个工具同式，这里取先遇到的那个。
_OWN_FAMILY_TOOL: dict[str, str] = {}
for _t2, _f2 in _OWN_TOOL_FAMILY.items():
    _OWN_FAMILY_TOOL.setdefault(_f2, _t2)

# 子句切分。**顿号 `、` 必须在内**：`_OWN_FAMILY_RE` 那几支的宾语 span 排除了中文逗号
# `，` 却**没排除顿号**，于是一句顿号并列的话会被整句读成**第一个子句的方向**——span
# 跨过 `、` 一直吃到第二个子句里的撤除词（实测：「把 12 收藏、把 13 取消收藏」被
# `_own_command(msg, "remove_favorite")` 读成 True、`add_favorite` 读成 False）。
# 那是既有判据的既有形状，**这里不动它**（它服务的是同意闸，"多判一次命令"的方向是
# 少一次点击）；本函数只是不拿它当"主人只说了反方向"的证据。
#
# 子句切分：**只切标点**（不含任何连词，见 `_own_families` 头注为什么连词不进这张表）。
_OWN_CLAUSE_SPLIT_RE = re.compile(r"[、，,；;。！？!?\n]+")


def _own_families(msg: str | None) -> set[str]:
    """这句命令了**哪几族** own-write（逐子句、子句内再逐起点判）。

    逐子句是必须的：几支族判据都锚在句首（`^`），整句扫会漏掉第二个子句里的那一族，
    而"第二个子句里还有另一族"正是多意图句的形状。漏了它，`action_conflict` 就会把
    一句**两个方向都说了**的话读成"只说了反方向"，从而拦掉一件主人真要的写。
    子句切分**必须在**（不是可有可无）：族判据的宾语 span 排除了中文逗号 `，` 却**没
    排除顿号**（既有形状，`_own_command` 那侧不动它）⇒ 不切的话「把 12 收藏、把 13
    取消收藏」会被整句读成**第一个子句的方向**，第二个子句那一族就此消失。

    **子句内逐起点**（20261016）：几支族判据锚在 `^`，而第二个子句可以以**连词**起头
    ——「把文章 12 取消收藏，**另外**把文章 13 收藏」里 `_OWN_ADD_RE` 的 `^把` 永远看
    不见后半句，那一族被判成"没说" ⇒ `action_conflict` 把一件主人**真要**的写拦掉。
    另一种写法是**再抄一份连词表**（另外/还有/以及/顺便…），而那是开放类：每遇一种新
    说法就要加一个词——本仓为这个病立了闸（`eval/wordlist_gate.py`：判据层的中文词表
    只许变小、不许长新的）。所以这里改**表征**：从子句内每个字起试一次，连词、语气词、
    任意口语前缀一律不必登记。

    代价是 O(子句长 × 族数) 次正则；消息都很短，且三族认全即提前退出。
    **多试几个起点只会多认出一族** ⇒ `action_conflict` 更容易返回 False ⇒ 退回既有的
    弹卡路径（fail-closed，多问主人一次），**不会**多拦一件主人真要的写；少认才是真
    损失（那才会拦掉主人真要的那件）。方向与全表一致：多一次点击 < 多一次误写。
    """
    text = strip_user_shell(msg)
    out: set[str] = set()
    for clause in _OWN_CLAUSE_SPLIT_RE.split(text):
        if not clause.strip():
            continue
        # 起点的**上界**：各族在句首的那次匹配，最远结束到哪儿。已经认出来的那条命令
        # **内部**不是另一条命令的起点——「取消收藏」里的「收藏」不是"要收藏"。这一格
        # 是逐起点扫的守卫：没有它，「取消收藏文章 12」会被读成"也说了收藏族"
        # ⇒ 反方向（主人说撤、planner 排了加）那一支当场失效。
        head = max([0] + [m.end() for m in (rx.match(clause)
                                            for rx in _OWN_FAMILY_RE.values()) if m])
        for i in range(len(clause)):
            if 0 < i < head:
                continue
            for fam, rep in _OWN_FAMILY_TOOL.items():
                if fam not in out and _own_command(clause[i:], rep):
                    out.add(fam)
            if len(out) == len(_OWN_FAMILY_TOOL):
                return out
    return out


def action_conflict(msg: str | None, tool: str | None) -> bool:
    """这一件写是不是**主人这句话要的那件的反面**（确定性、无 LLM）。

    两个条件同时成立才算冲突：主人这句话**明确命令了**它的反极性兄弟，**且没有**
    命令它自己。前半句说"主人要的是那件"，后半句说"主人没要这件"——两句都是
    `_own_command` 的现成用法（它判的就是"命令**这一个**工具"）。

    多意图句（「把 12 收藏、把 13 取消收藏」）**不会**在这里被拦：`_own_command`
    里那条"同一句里出现了另一族 ⇒ False"让两句都判 False ⇒ 本函数 False ⇒ 照旧
    走弹窗（§1.57 点名必须走查的那个反例，由 `_own_command` 自己的 fail-closed
    兜住，不是由这里兜）。反极性兄弟服务的是**另一个目标**时同理。
    """
    sib = own_antipode(tool)
    if not sib:
        return False
    if not _own_command(msg, sib):
        return False
    if _own_command(msg, tool):
        return False
    # 这句里**还出现了这一件所属族**的动作（逐子句扫，见 `_own_families` 的头注）⇒
    # 两个方向都说了（多意图句），判不准 ⇒ **不拦**，交给既有的弹卡问一次。
    # §1.57 点名必须走查的反例就是它：「把 12 收藏、把 13 取消收藏」——反极性兄弟
    # 服务的是**另一件目标**，那不该被摘。
    fam = _OWN_TOOL_FAMILY.get(tool or "")
    return fam not in _own_families(msg)


REASON_ACTION_CONFLICT = "action_conflict"


def action_conflict_frame(tool: str, sibling: str | None = None) -> str:
    """反极性改选时的错误帧（checker 判 BLOCK，planner 按帧改回来）。

    形态与 `adminops.target_conflict_frame` 同族（`__ERROR__` + 原因码），论证也
    同一条：**问错一件再让主人点确定，等于把误靶洗成一条已授权的写**——只不过
    target 那一族洗错的是"哪一篇"，这一族洗错的是"哪一边"（收藏 ↔ 取消收藏）。

    帧文里那一句"不许把相反的那件当成同一件事办掉"是本格存在的理由：模型改选
    反极性兄弟不是因为不懂，而是因为**菜单里只剩它**（见 `denied_workflows`）——
    它需要有人明说"这不算办成了"。

    帧里点的是**工具名**而不是中文动作词：读它的是 planner，而 planner 的 TOOLS 行
    本来就只认工具名（另立一份"工具 → 中文动词"表的话，它既多一张要同步的中文表、
    又会漂——`eval/wordlist_gate.py` 正是为"判据层不许长新词表"立的闸）。
    """
    sibling = sibling or ""
    return (f"__ERROR__: 与主人这句话要的动作相反[{REASON_ACTION_CONFLICT}]"
            f"（主人这句明确要的是「{sibling}」，而你排的是「{tool}」"
            f"——两件事相反。下一轮按主人说的那件重新规划；"
            f"**办不成也如实告诉他没办成**，不要拿相反的那一件来充数）")


_OWN_ACTION_ERR_RE = re.compile(rf"\[{REASON_ACTION_CONFLICT}\]")


def action_conflict_error_reason(text: str) -> str | None:
    """从错误帧取回原因码（非本族帧 → None），供 `_check_spec` 用。"""
    return (REASON_ACTION_CONFLICT
            if _OWN_ACTION_ERR_RE.search(str(text or "")) else None)


# ── 「在问我自己那份数据」的问句判据（20261003）─────────────────────────────
# 方向与上面那一族**相反**：`_own_command` 判"这句是不是一条写命令"（判错的代价是多问一次
# 点击），这条判"这句是不是在问**我自己账号里的数据**"（判错的代价是让 planner 白跑一轮）。
# 消费方**两个**（20261004 判据前移）：`graph.planner_node`（决策层：这一轮点了 `chat`
# 而主人在问自己那份数据 ⇒ 先走一次性纠偏，`data_question_no_tool`）与
# `graph.gate_node` 第 4 节（闸门层：planner 纠偏后仍零帧才轮到它）。判据本身一个字
# 没改——前移只是把同一条判据挪到**更早的那一层**，闸门那一条原样留着当兜底。
#
# 现场（uid=1 会话 320，trace `20261003T194144`）：主人问「我有哪些未读通知呀」，planner 落
# chat 零工具，narrator 回了一段**关于上一轮话题（翻日志 / worker respawn）的真话**——每一句
# 都经得起核，于是 gate 的判据族（全是"这句话真不真"型）全绿放行，主人只能下一轮自己纠错。
#
# **为什么不写"回复与问句的主题交集为空"那条通用判据**：全量回放 uid>0 的零帧放行轮，大段
# 合法闲聊（「小猫咪你会做蛋糕吗」「我刚刚说要，是要什么」「小猫咪按你想法来吧」）与问句的
# 字符 bigram 覆盖率**就是 0.00** ⇒ 拿主题交集当判据等于把闲聊全判死。所以这里不碰"主题"，
# 只判**前置条件**：主人问的是自己那份数据（站内为它备了专门的无参取数工具），而这一轮
# 一个字节都没取。判据只管打回去重规划一次，**不替 planner 选技能**（与 `_REPLAN_*` 同纪律）。
#
# 五步（每一步都被全量语料量过，成对表见 `tests/test_authz.py`）：
#   ① 剥壳（`strip_user_shell`）：判据看到的是主人实际说的那句；
#   ② 能力问句排除——「你能发私信吗」「你可以单独把一个消息标记已读吗」问的是"能不能"，
#      如实回答"不能"就是**对的出路**，打回去只会换个说法（同族取舍见洞⑫）；
#   ③ 写命令排除——「我的未读信息全部就标记为已读」归写侧（洞⑨ `sys_write_claim_without_tool`），
#      这里放它过去等于替另一族的现场抢答；
#   ④ 必须是问句——`is_question_like` **认不出**「我有哪些未读通知呀」（它认 吗/呢/怎么，
#      不认"呀"），所以必须并上现成的量词型打听表 `_OWN_INQUIRY_RE`（哪些/几条）：少了这一步
#      **漏掉的正是事故原句本身**；
#   ⑤ 物件要是自己那份——私信/站内信/信箱/未读/红点 自带"我的"语义，直判；提醒/通知/公告/
#      消息/信息/收藏 是通用词（「站内最近有什么公告吗？」是**公开面**，golden 有专条），
#      必须同时出现自指（我/咱/俺/自己）。
#
# 射程（落码前先量，全量 1106 份 trace 回放）：uid>0 的零帧放行轮 299 轮里命中 **2**——
# 一条就是上面那次事故的原句；另一条是 20260923 的纠正轮「不应该是私信内容吗要看吗」（回复
# 本身没说错话，但"去查私有收件箱"确实是更对的出路）。golden 155 条命中 2 条，**两条都是
# uid=0 的 not_logged_in 用例** ⇒ 由调用侧的 `uid > 0` 守卫排除，默认跑 0 次开火。
_OWN_SELF_RE = re.compile(r"我|咱|俺|自己")
_OWN_PERSONAL_OBJECT_RE = re.compile(r"未读|红点|信箱|私信|站内信")
_OWN_BROAD_OBJECT_RE = re.compile(r"提醒|通知|公告|消息|信息|收藏")
# 能力问句：前一支（能不能/可不可以…）**独自成问**，不要求句尾有"吗"——生产原话
# 「能不能替我发个公告」整句没有"吗"；后一支要求句尾问号/语气词，中间允许 20 字
# ——「你可以单独把一个消息标记已读吗」中间隔了 11 个字。
_OWN_CAPABILITY_RE = re.compile(
    r"能不能|可不可以|能否|会不会|行不行"
    r"|(?:能|可以|会|支持)[^。！？!?；;\n]{0,20}?(?:吗|嘛|么|？|\?)")


def is_own_read_question(msg: str) -> bool:
    """这句是**在问我自己账号里的数据**吗（未读通知 / 站内信 / 私信 / 收藏…）？

    只回答"这是不是一个**该去取数**的问句"，不回答"该取哪一份"——落到哪个技能由 planner 定
    （同 `_replan_note` 的纪律：这一层只给路径、不给结论）。空消息 → False（无从句可判）。
    """
    text = strip_user_shell(msg)
    if not text:
        return False
    if _OWN_CAPABILITY_RE.search(text):            # ② 能力问句：如实答"不能"就是出路
        return False
    if any(_own_command(text, tool) for tool in _OWN_TOOL_FAMILY):   # ③ 写命令归写侧
        return False
    if not (is_question_like(text) or _OWN_INQUIRY_RE.search(text)):  # ④ 问句（含量词型打听）
        return False
    if _OWN_PERSONAL_OBJECT_RE.search(text):       # ⑤ 自带"我的"语义的物件
        return True
    return bool(_OWN_BROAD_OBJECT_RE.search(text) and _OWN_SELF_RE.search(text))


def is_own_write_command(msg: str) -> bool:
    """这句是不是一条「写**我自己**账号里数据」的命令（收藏 / 取消收藏 / 标记已读）？

    **不是新判据**：它就是同意闸那一条 `_own_command`（`_CONSENT_PATTERNS[SCOPE_WRITE_OWN]`）
    的**族级形态**——`_own_command` 判的是"命令**哪一个**工具"（它必须拿到工具名，
    见那一段的长注），而这一层的消费方是 planner 的"写命令却零调用"纠偏，问的是
    "这句话是不是在命令我改自己的数据"，此时**还没有**工具名可给，所以对
    `_OWN_TOOL_FAMILY` 里每个工具各判一次再取或。那张表是工具→家族的**完备映射**
    （未登记的工具一律 False），所以这里遍历表本身，不另列名单。

    与 `is_own_read_question` 是同一族的两半：那条 ③ 把写命令排除出去，这条就判它。
    空消息 → False。
    """
    return any(_own_command(msg, tool) for tool in _OWN_TOOL_FAMILY)


# ── 「话题落在站内语料上，却零检索」的问句判据（20261004）──────────────────────
# 与 `is_own_read_question` 互补：那条问"是不是在问我**自己账号**里的数据"（私有面），
# 这条问"是不是在问**站内语料**里的东西"（公开面：文章 / 教程 / 文档 / 专栏 / 归档…）。
# 消费方同样**两个**（20261004 判据前移）：`graph.planner_node` 与
# `graph.gate_node` 第 4 节——分工与上一条判据同款（决策层先拦、闸门兜底）。
#
# 要治的病（**语句**是生产原话，参见下面「现场写法的一次自我更正」）：主人问站内有些什么，
# 而 narrator 回「**我这边没有工具可以帮你查具体的文章标题和链接**，毕竟我只是个负责陪聊的
# 看板娘」——站里明明备着 `rag_search` / `search_notes` / `list_notes`。gate 既有的判据族全是
# "这句话真不真"型（它还真没撒谎：**手上**确实没有帧），于是零帧放行。
#
# ⚠️ **现场写法的一次自我更正（20261004 落码当天，同一批 trace 复扫时发现）**：这条判据落码时
# 引的现场是 `20261002T195955_9_r8d7`，并把它写成"planner 落 chat、零工具、零帧"。**那句是错的**：
# 那条 trace 的 `user_id=9` 是**杂鱼**（`CHAT_ONLY_ROLES`），planner 节点在 `zako_shortcut` 处就
# 短路了（trace 里**没有 `decision` 事件**，只有 `zako_shortcut`），计划 status 是 `wrapped` 而不是
# `answer_only`；对那个身份"手上没有工具"是**结构上的事实**（它本来就是零工具身份），所以那句回复
# 不是缺陷，gate 放行也是对的。教训与 `docs/问题记录.md` 同族：**引用一条 trace 当现场，必须
# 连"这条轮走的哪条路径"一起核实**——只读回复文本会把它读成"planner 偷懒没检索"。
# 落到数字上（全量 384 份 trace、92 轮 uid>0 零帧放行轮复扫，见下方射程）：判据词面命中 **1**
# 条，正是这条杂鱼轮；而 gate 里那条 `status == "answer_only"` 守卫把它**结构性地**排除在外。
#
# **为什么不能写成通用形态**（"BM25 有候选就开火"）：全量回放 uid>0 的零帧放行轮 **300** 轮，
# 通用形态开火 **211** 轮——大段合法闲聊（「好猫猫，亲一个」「去给我炒两个菜」）与语料本来
# 就有 2-gram 重叠，分不开。阈值臂（top1≥8）开火 22 轮，逐条看多为写命令 / 能力问句 / 元讨论。
# 所以判据收成**站内指称臂**：主人**明说**了"站内/博客里/你的文章/专栏…"才算。
#
# 六步（每一步都被全量语料量过，成对表见 `tests/test_authz.py`）：
#   ① 剥壳（`strip_user_shell`）：判据看到的是主人实际说的那句；
#   ② 能力问句排除——同 `is_own_read_question` 第 ②（如实答"不能"就是对的出路）；
#   ③ 元问句排除——「你用哪些工具读文章」问的是"你有什么"，不是"给我一份数据"，
#      打回去只会换个说法；
#   ④ 自指数据排除——通知/私信/收藏…是 `is_own_read_question` 的现场，别抢那一族的活；
#   ⑤ 指代型排除——「那个分类下面有几篇文章」按 rule 6b 该**照抄跨轮执行摘要的取值**、
#      零工具才是对的（去检索反而绕远）。golden `followup_entity_slot_category` /
#      `_ambiguous` 正是这一条：它们与"真的要检索"字面完全同形（top1 都是 10.33），
#      少了这道守卫就直接打红一条现在绿的用例；
#   ⑥ 站内指称 + 必须成问（含量词型打听）+ 语料确有候选（`rag.search` 的 BM25 命中，
#      `_CLIFF_RATIO` 已在内）。
#
# 射程（**落码时先后量过两遍**）：
#   · 判据词面（落码前，全量 1110 份 trace 回放）：uid>0 的零帧放行轮 300 轮里命中 **1**；
#     未加 ④⑤ 两道守卫时是 4 轮（另外那三条是跨轮取值 / 自指数据）。这个 1 **不是真阳性**，
#     理由见上面的自我更正——它是那条杂鱼轮。
#   · **gate 真正的射程（落码后复扫，全量 384 份 trace、92 轮 uid>0 零帧放行轮）：0**。
#     唯一的词面命中被 `status == "answer_only"` 守卫排除（杂鱼的计划是 `wrapped`）。
#     也就是说这条判据今天在**生产上一次都不会开火**——它是一张备用网，不是补丁；
#     它保护的是"普通/管理员身份的纯作答轮声称手上没有工具"这个**形状**，
#     该形状由 `tests/test_gate_replan.py` ⑥d（uid=5「站内最近有什么公告吗？」真去检索）
#     与 ⑥e 的杂鱼对照（role=zako 同一句 ⇒ 放行）两条锁住。**零开火不等于可以删**：
#     它的红从没被生产触发过，这必须是写下来的事实，而不是"它一直在保护我们"的错觉。
#   · golden 156 条命中 14 条：12 条要求工具调用（有帧 ⇒ gate 第 4 节结构上摸不到），
#     `admin_notes_console_list`（needs_admin_uid，uid>0 但必跑 `list_admin_notes` ⇒ 有帧），
#     `admin_user_list_denied_visitor`（context 不写 uid ⇒ uid=0 ⇒ 被调用侧的 `uid > 0` 排除；
#     它是**诚实拒绝**用例，站里没有它能查的名录，本就该零工具）。词面上的命中面比"只有检索
#     用例"宽（含公告/分类/标签/置顶这类**清单型**站内数据），这正是调用侧 `uid > 0` 之外
#     还要靠"有帧就不开火"兜住的原因。
_SITE_DEIXIS_RE = re.compile(
    r"站内|站里|站上|博客里|博客中|你博客|本博客|你的博客|你的文章|你写的|你发的"
    r"|专栏|归档|技术文档|你的文档|这篇|那篇|哪篇|文章|教程|文档|笔记")
# 元问句（问"你怎么读 / 你有什么工具"）：不是"要一份数据"，打回去只会换个说法
_SITE_META_RE = re.compile(
    r"工具|函数|接口|API|调用|技能|插件"
    r"|你(?:是|会|能)?(?:怎么|如何|用哪|用什么|拿什么)")
# 自指数据（私有面）：归 `is_own_read_question`，这里放行
_SITE_OWNDATA_RE = re.compile(r"未读|红点|信箱|私信|站内信|通知|收藏|我的消息|我的留言")
# 指代型（rule 6b 取值指代）：照抄跨轮执行摘要才是对的出路
_SITE_DEICTIC_RE = re.compile(
    r"那个|这个|那一?篇|这一?篇|那篇|这篇|那条|这条|它|上面|刚刚|刚才|上次")


def is_site_corpus_question(msg: str) -> bool:
    """这句是**在问站内语料里的东西**（文章 / 教程 / 文档 / 专栏…）吗？

    只回答"这是不是一个**该去检索一次**的问句"，不回答"该检索什么"——落到哪个技能由 planner
    定（同 `is_own_read_question` 与 `_replan_note` 的纪律：这一层只给路径、不给结论）。
    空消息 → False。语料候选由 `rag.search` 判（**惰性导入**：别让 authz 的模块加载
    去背语料索引的构建代价，也被测试挡在门外时仍可单独用）。
    """
    text = strip_user_shell(msg)
    if not text:
        return False
    if _OWN_CAPABILITY_RE.search(text):                       # ② 能力问句：答"不能"就是出路
        return False
    if _SITE_META_RE.search(text):                            # ③ 元问句
        return False
    if _SITE_OWNDATA_RE.search(text) or is_own_read_question(msg):
        return False                                          # ④ 自指数据归另一族
    if _SITE_DEICTIC_RE.search(text):                         # ⑤ 指代型：照抄跨轮摘要 (rule 6b)
        return False
    if not (is_question_like(text) or _OWN_INQUIRY_RE.search(text)):   # ⑥a 必须成问
        return False
    if not _SITE_DEIXIS_RE.search(text):                      # ⑥b 站内指称
        return False
    from rag.search import search as _rag_search              # ⑥c 语料确有候选
    try:
        return bool(_rag_search(msg))
    except Exception:                                         # 索引没热起来/坏了 → fail-closed
        return False


def scopes_for(role: str | None) -> frozenset[str]:
    """角色 → 授予的 scope 集。未知角色（含 None）→ 空集。"""
    return _ROLE_SCOPES.get(role or "", frozenset())


def required_scope(tool: str) -> str | None:
    """工具所需的 scope；未声明 → None（enforce 下按拒绝处理）。"""
    return TOOL_SCOPE.get(tool)


def is_write(tool: str) -> bool:
    """是否写操作（留给"人在回路确认"的挂钩，现在只用于观测/标注）。"""
    return TOOL_SCOPE.get(tool) in WRITE_SCOPES


def requires_consent(principal: Principal | None, tool: str) -> bool:
    """这个工具这一次要不要"人在回路"确认（与 principal 无关：确认是用户的事）。

    只看 scope 是否在 CONSENT_SCOPES ——**声明驱动**，所以将来新增一个取
    需确认 scope 的工具会自动落在闸下，不需要有人记得来改这个函数。
    """
    return required_scope(tool) in CONSENT_SCOPES


def consent_granted(principal: Principal | None, tool: str, user_msg: str) -> bool:
    """用户本轮消息里有没有对该 scope 的明确确认（确定性、无 LLM）。

    fail-closed：需确认的 scope 若没配确认语表 → **False**（绝不默认放行）；
    消息为空 → False。表值可以是正则（`.search`）或谓词（直接调用），见
    `_CONSENT_PATTERNS` 的注释。

    谓词收 **`(msg, tool)` 两个参数**（20260923）：一个 scope 下可能挂着多个工具
    （`write.own` 现在是收藏/取消收藏/标记已读三件），而同意闸是按 scope 查表的——
    判据只看到消息的话，"用户命令 A、planner 却填了 B"这种**误靶写**会被 scope 级
    同意放过去。给了工具名，`_own_command` 才判得了"这句是不是在命令**这一个**工具"。

    `_ALWAYS_CONFIRM_TOOLS` 里的工具**永不走"同轮命令即确认"**（见那张表的注释）。
    """
    if tool in _ALWAYS_CONFIRM_TOOLS:
        return False
    spec = _CONSENT_PATTERNS.get(required_scope(tool) or "")
    if spec is None:
        return False
    msg = user_msg or ""
    if callable(spec):
        return bool(spec(msg, tool))
    return bool(spec.search(msg))


def manifest_gaps(tool_names) -> list[str]:
    """哪些工具没在 TOOL_SCOPE 里声明（完备性断言用，CI 层拦新增工具的疏漏）。"""
    return sorted(n for n in tool_names if n not in TOOL_SCOPE)


def manifest_stale(tool_names) -> list[str]:
    """TOOL_SCOPE 里声明了但注册表里已没有的工具（改名/下线后残留）。"""
    known = set(tool_names)
    return sorted(n for n in TOOL_SCOPE if n not in known)


# ── 判据 ─────────────────────────────────────────────────────────────
REASON_OK = "ok"
REASON_UNKNOWN_ROLE = "unknown_role"   # 身份不明 → 零权限
REASON_NO_MANIFEST = "no_manifest"     # 工具未声明 scope（fail-closed）
REASON_DENIED = "denied"               # 角色已认，但这个 scope 没授予
REASON_CONSENT = "consent_required"    # 有权做，但用户本轮没有明确确认（写操作）


@dataclass(frozen=True)
class Decision:
    allowed: bool
    reason: str
    scope: str | None = None
    tool: str = ""

    def __str__(self) -> str:
        if self.allowed:
            return f"allow {self.tool} scope={self.scope}"
        return f"deny {self.tool} reason={self.reason} scope={self.scope or '-'}"


def check(principal: Principal | None, tool: str) -> Decision:
    """唯一的权限判据：这个 principal 能不能调这个工具。

    纯函数（不看 settings、不看时间）——enforce 与否由调用方决定（`enforcing()`），
    这样"算出来的决策"可以在 shadow 模式下被记录、被回归测试，而行为不变。
    """
    scope = required_scope(tool)
    if scope is None:
        return Decision(False, REASON_NO_MANIFEST, None, tool)
    role = principal.known_role if principal else None
    if role is None:
        return Decision(False, REASON_UNKNOWN_ROLE, scope, tool)
    if scope in scopes_for(role):
        return Decision(True, REASON_OK, scope, tool)
    return Decision(False, REASON_DENIED, scope, tool)


def holds(principal: Principal | None, scope: str) -> bool:
    """这个 principal 有没有这个 scope —— **给"工具之外的能力"用**（20261003）。

    为什么不是 `check()`：那张表是「工具 → scope」（`TOOL_SCOPE` 的键必须与
    `_TOOL_REGISTRY` 一一对应，`tests/test_authz.py` 的完备性断言盯着它）。后台的
    「重建向量图谱」是一个**端点级**能力（一次长任务 + 轮询，不是一轮对话里的一次
    工具调用），它没有工具条目、也不该有——为它往 `TOOL_SCOPE` 里塞一个假工具名，
    等于把"工具注册表"这张表变成"工具 + 端点"的杂表，而完备性断言正是靠"键即工具"
    这条性质在守漏项。

    所以这里只做**授予表查询**那一半（`scopes_for`，唯一事实源不变），决策的形态
    照旧：身份不明（role=None / 不认识）⇒ 零权限，从不默认放行。
    """
    role = principal.known_role if principal else None
    return role is not None and scope in scopes_for(role)


def enforcing(scope: str | None = None) -> bool:
    """这个 scope 是否真的拦（默认 False = shadow：只算不拦）。

    `scope=None` = 沿用旧的"全局开关"语义（不知道 scope 的调用点用它）。
    传了 scope 且它在 `_HARD_SCOPES` 里 → 恒 True（见该常量的注释）。

    ⚠️ **拦截判据点不要直接调它**——用 `must_enforce(decision, principal)`。这个函数
    只看 scope，看不见"这次是谁在调"，而有一类身份是**不该有灰度期**的（见下）。
    """
    if scope in _HARD_SCOPES:
        return True
    from config.settings import settings
    return bool(getattr(settings, "authz_enforce", False))


# 未验成的身份**唯一保留的那一面**（见 `must_enforce`）：公开内容 + 它自己那一屏。
#
# 为什么是这两条：它们恰好是**不需要身份**的那两件事——读公开内容本来就匿名可读
# （访客不登录也能看文章），`write.page` 的效果只是一条发给**请求方自己浏览器**的
# 命令（导航/特效/夜间模式），不落库、不碰别人的东西、不碰物理世界。
# 反过来，`read.own`（别人的收藏/通知/会话历史）、`write.own`（往别人账号里写）、
# `write.device`（物理动作）、两个后台 scope —— 每一条都以"我们知道你是谁"为前提，
# 而回退身份**没有**这个前提。
_PUBLIC_SURFACE = frozenset({SCOPE_READ_PUBLIC, SCOPE_WRITE_PAGE})


def must_enforce(decision: Decision, principal: Principal | None) -> bool:
    """**这一次的拒绝**要不要真的拦——四个拦截判据点的唯一入口（20261019）。

    与 `enforcing(scope)` 的分工：那个回答"这个 scope 参不参与灰度"（只看 scope），
    这个回答"这一次的拒绝拦不拦"（多看一眼 principal）。四个调用点（graph 三处：
    硬拦 / shadow 记账 / 弹窗前置筛，react_line 一处）**都换成它**——直接问
    `enforcing` 会把下面这一类漏过去。

    ── 为什么"未验成的身份"没有灰度期（安全审计 SAU-005 的纵深那一半）──
    shadow 的正当性建立在一个前提上：**被观测的能力本来就在跑**，所以"先只记不拦"
    是在测绘既有流量，放行错了顶多多做一件本来也会发生的事。
    `source == SOURCE_BODY` 的身份不满足这个前提，而且差得比"纯新增能力"更远——
    它**不是另一种用户，是身份没验成**：`server._resolve_principal` 在断言缺席/验签
    失败时把 `body.user_id` 原样当身份（`role=None` ⇒ 零权限），也就是说这个 uid 是
    请求方自己写的一个整数。以它为 principal 的每一次 deny 都恰好是"我们根本不知道
    你是谁"——而 shadow 会把它**放行**，于是 uid 即身份、水平越权成立（审计给的复现
    就是一句 curl）。所以这一类在**私有面与写面**上一律硬拦，只留 `_PUBLIC_SURFACE`
    （读公开内容 + 驱动它自己那一屏）——见那个常量的注释。

    落点值得写清楚：`server._resolve_principal` 的回退分支**恒给 `role=None`**
    （"读不到角色就绝不默认授予任何权限"），而 `check()` 对 role=None 判零权限 ⇒
    这个身份的**每一次**工具调用都是 deny，于是被 shadow 放行的不是"某几条敏感
    scope"，而是"身份不明"这个结论本身——`check` 说零权限、`enforcing` 说先别拦，
    两句话在阴影里同时成立了一整个观测期。两个开关各管各的：出厂默认已经把
    `AGENT_REQUIRE_ASSERTION` 翻成 1（SAU-005 的主体），这一条是**独立的第二道**，
    谁把那个开关拨回去都还拦得住。

    ⚠️ 判据是 `source`，**不是** `role is None`：`source="golden"` 的评测身份同样
    `role=None`（`eval/run_golden.py::build_principal`，用例不写 `context.role` 就是
    它），拿 role 判会把一整族既有用例从"shadow 放行"打成 BLOCK——那是判据基线的
    移动，不该由一次安全加固顺带做掉。`source` 是**身份怎么来的**，正是这里要问的。

    已放行的决策一律 False：本函数只回答"拒绝拦不拦"，调用点若漏写
    `not decision.allowed` 也不至于给一次合法调用产拒绝帧（fail-safe 方向）。
    """
    if decision.allowed:
        return False
    if (principal is not None and principal.source == SOURCE_BODY
            and decision.scope not in _PUBLIC_SURFACE):
        return True
    return enforcing(decision.scope)


def denial_frame(decision: Decision, principal: Principal | None) -> str:
    """拒绝时的 __ERROR__ 帧文本。形态与参数引用失败同族，带原因码——
    planner 据此如实告知（不是"系统故障"，是"你的身份不允许"）。"""
    who = f"uid={principal.uid} role={(principal.role if principal else None) or '未知'}"
    return (f"__ERROR__: 权限不足[{decision.reason}] —— {who} 无权调用 {decision.tool}"
            f"（需要 {decision.scope}，身份声明不含它；不要换工具绕，如实告知用户）")


_SCOPE_ERR_RE = re.compile(r"权限不足\[([a-z_]+)\]")


def scope_error_reason(text: str) -> str | None:
    """从 __ERROR__ 帧文本里取回权限拒绝的原因码（execute 产帧 → checker 判 reason）。

    与 refs.ref_error_reason 同形（帧格式：`__ERROR__: 权限不足[<原因码>] —— …`），
    让拒绝在受阻链路里带上可判读的原因，而不是笼统的 error_frame。
    """
    m = _SCOPE_ERR_RE.search(text or "")
    return m.group(1) if m else None


# 每个需确认的 scope 的"未获确认"说明——**这段文字会被 planner 读进去、并照着
# 向用户复述**，所以必须说准这次到底要确认什么：把"隐藏一篇文章"说成"把内容发布
# 出去"，用户会以为 agent 理解错了指令（20260921 加 write.console 时拆出来）。
_CONSENT_WHY = {
    SCOPE_WRITE_CONSOLE: (
        "会改动站点上的文章状态/标签（对外可见，且不会自动复原）",
        "请把**你打算改什么、改成什么**原样告诉用户（哪一篇、从什么变成什么），"
        "并请他明确说一句命令（例如「把文章 12 设为私密」）；"
        "若他只是在提问或假设，先回答他的问题，不要执行"),
    SCOPE_WRITE_OWN: (
        "会写进用户**自己账号**里的状态（收藏、通知已读；只有他自己看得见，可撤销）",
        "请说清楚**你要动哪一篇/哪些通知**（收藏还是取消收藏、哪篇文章），"
        "并请他明确说一句命令（例如「收藏这篇」「把通知都标记已读」）；"
        "若他只是在问有哪些收藏、或者在问怎么用，先回答，不要写"),
}


# 按**工具族**的文案覆盖（20260925）：上面三张按 scope 给的措辞都是**文章族**口径
# （"哪一篇、从什么变成什么（例如「把文章 12 设为私密」)"），而一律弹窗族里的公告
# 根本不是"改哪一篇"。生产 trace 20260924T234402 的代价是实测到了的：公告被挡时模型
# 收到的是文章族措辞，读成"内容不合规"，连着四轮改写公告正文——其中一轮开始**替主人
# 编造原话**去够同意闸。那一轮里这段文案是系统唯一给模型的话，它必须说这件工具真正
# 要做的事，并且把"别再改内容"讲成下一步动作（否则模型无从知道"重写"不是出路）。
#
# 覆盖按**工具名**取、按 scope 兜底：新增一律弹窗的工具若忘了登记，落到的是 scope 那张
# （文章族措辞），不会没文案——方向上只是措辞不贴，不是静默放行。
#
# ── 20261009（乙）：把"等他点确认"改成"请他明确回一句话要办" ─────────────────
# 这 15 处尾巴原文都是「然后等他点确认——」。改它的两条实测依据（先记一条**我猜错的**，
# 免得后人照错的说法去改判据）：
#
#   1. ❌ **它本身并不命中洞⑥ 的正则**（实测：17 条 how 文案跑 `_confirm_claim_clause`，
#      ① 支 0 命中）。①b 的人称槽只认 `主人|你|您|访客`，而这里写的是"他"；①a 要求
#      "点(确定|确认)"后面**紧跟**"我/就/即/便/后"，而这里紧跟的是破折号。所以这不是
#      "系统印了一句可抄的违禁句"，而是**系统教了一个形状**——narrator 把它展开成自己的
#      话时才长成违禁句。现场 `20261009T045019` 展开成的是"点确认我就去勾掉它"（命中）。
#   2. ✅ **但这句话在它唯一的使用路径上是假的**：`consent_frame` 只在
#      `consent_required` 那一支产帧，而那一支的前提恰恰是**本轮没有卡**
#      （要弹卡时 `_confirm_popup` 直接返回 `pending_confirm`、整轮 END，走不到执行）。
#      于是这段文案一边让模型"念卡面"，一边让它"等他点确认"——**两块屏幕上都不存在的东西**。
#      主人问"还会不会有影响"这类**提问**尤其明显：`_confirm_popup` 见提问直接不弹卡，
#      所以提问轮 100% 落在"没有卡却说等点确认"这一档。
#   ⇒ 改法：尾巴改成"**请他明确回一句话要办**"——这是这一轮**真的**需要的下一步
#      （也是 `_CONFIRM_PROMISE_PAT` 那条"有据的承诺形态"所对应的动作），不再指一个
#      不存在的按钮。洞⑥ 那边 20261009 另加了前提闸（同意闸拦过就不判 ① 支），
#      **文案与判据各改一处、互不依赖**：文案管预防，判据管兜底。
#
# 已知残余（登记不修，未取证）：15 处前半句仍写「卡面上还有它的 id 与当前状态」——
# 同一路径下"卡面"也不存在。它**不在洞⑥ 射程内**（② 的名词表是 确认框|确认弹窗|弹窗，
# 不含"卡面"），改它要逐条重写这 15 条的前半句（那半句承载的是"要念哪些字段"的信息，
# 删不得），收益未取证，故不动。
_CONSENT_WHY_TOOL = {
    "create_announcement": (
        "会发布一条**全站访客都会看到**的公告（首页可见，删掉不会自动回来）",
        "请把**标题与正文全文**原样念给主人看（正文可以是你按他的意思整理过的那一版，"
        "但他必须看到**完整**的那一句——那是要署他名义对全体访客说的话），"
        "然后**请他明确回一句话要办**——"
        "**这一轮到这里就够了：不要再改内容，也不要换个写法重试**"),
    "update_announcement": (
        "会改动一条**全站访客都会看到**的公告（首页可见）",
        "请把**改后的标题与正文全文**原样念给主人看（同上：可以按他的意思整理，"
        "但卡面上必须是完整的那一句），然后**请他明确回一句话要办**——"
        "**不要再改内容，也不要换个写法重试**"),
    "delete_announcement": (
        "会删掉一条**全站访客都会看到**的公告（删了没有回收站）",
        "请把那一条公告的**标题与正文原文**一字不改地念给主人看，"
        "并请他明确说一句命令——不要再换个写法重试"),
    "delete_board_comment": (
        "会删掉**访客写下的一条留言**（删了没有回收站）",
        "请把**那条留言的 #id、作者与原文**一字不改地写出来，"
        "并请他明确说一句命令——不要替他改写留言内容"),
    # 复核那两句话必须**同时**说出两个方向（通过=对全体访客显示 / 驳回=只藏起来），
    # 不能只写一半：方向由 verdict 定，而这一条 why 是两向共用的模板；只写一个方向
    # 会让主人按相反方向的那一句去核对。两条共同的后果 = **留言本身不会消失**
    # （可改判）——这也正是它与上面删除那一条的分界，写清楚他才知道该用哪一件。
    "audit_board_comment": (
        "会把那条留言**对全体访客放行显示**（通过）或**只对它自己可见**（驳回）"
        "——两种都是**可改判**的（驳回的能再放行），留言本身**不会消失**",
        "请把卡面上那条留言的**作者与原文**一字不改地念给主人看，"
        "并说清这一下是**放行还是驳回**，然后**请他明确回一句话要办**——"
        "**这一轮到这里就够了：不要改 verdict，也不要换个说法重试**"),
    "create_dashboard_todo": (
        "会往主人**后台首页的待办列表**里加一条（那是他自己那份列表，加完可改可删）",
        "请把**这条待办的正文与排期日**一字不改地念给主人看（排期按「X月X日」说，"
        "他没说日期就说「未排期」），然后**请他明确回一句话要办**——"
        "**这一轮到这里就够了：不要再改写正文，也不要换个说法重试**"),
    "complete_dashboard_todo": (
        "会把主人**后台首页的待办列表**里那一条的**完成标记翻过来**"
        "（正文与排期一个字都不动，这一下不新增也不删除任何一条）",
        "请把**那一行的正文一字不改**地念给主人看（卡面上还有它的排期与当前状态，"
        "让他核对是不是那一条），然后**请他明确回一句话要办**——**这一轮到这里就够了："
        "不要换个写法重试，也不要把这理解成「再记一条」**"),
    "reschedule_dashboard_todo": (
        "会把主人**后台首页的待办列表**里那一条的**排期日改掉**"
        "（正文与完成标记一个字都不动，这一下不新增也不删除任何一条）",
        "请把**那一行的正文一字不改**地念给主人看，并说清**它现在排在几号、"
        "这一次要改成几号**（主人说的日子没定下来时不要替他挑；"
        "他说不要排期了就是「改成未排期」），然后**请他明确回一句话要办**——**这一轮到这里就够了："
        "不要换个写法重试，也不要把它当成「再记一条待办」或「勾完成」**"),
    # 冻结与解冻的 why **必须不同形**，而且差异点要落在**后果**上而不是动词上
    # （「会冻结」/「会解冻」等于没说）：一个说的是"会话全没了且解冻也换不回来"，
    # 一个说的是"他这才重新能登录"。两条都**不许**承诺"恢复原状"——`token_version`
    # 只增不减，解冻恢复的只是"能不能登录"（父仓 `scripts/probe_token_revoke.py` 的【三】）。
    "freeze_account": (
        "会把那个账号**当场踢下线**（他所有已登录的会话立刻失效，且解冻也换不回"
        "那批会话，他得重新登录）",
        "请把**账号名一字不改**地念给主人看（卡面上还有它的 id 与当前状态，"
        "让他核对是不是那个人），然后**请他明确回一句话要办**——**这一轮到这里就够了："
        "不要替他换一个账号，也不要换个说法重试**"),
    "unfreeze_account": (
        "会让那个账号**重新能登录**（他此前被踢下线的会话不会自动回来，"
        "要他本人重新登录一次）",
        "请把**账号名一字不改**地念给主人看（卡面上还有它的 id 与当前状态，"
        "让他核对是不是那个人），然后**请他明确回一句话要办**——**这一轮到这里就够了："
        "不要说成「恢复原状」，也不要换个说法重试**"),
    # 变更身份的 why 与冻结族**必须不同形**，而且**不能只写要改成的那一档**——
    # 这一件与冻结族最容易被读混（两个都"当场踢下线"），差异在**方向**上：
    # 冻结是单向的"关掉"，这一件是**换档**，从哪一档到哪里都可能，读起来的语气
    # 完全相反（降成杂鱼是收走能力，升回普通用户却是把能力还给他）。所以 why 的骨架
    # 是"**他现在是什么 → 会变成什么**"两半都给，后果句随目标档位变（杂鱼 = 只剩闲聊；
    # 普通用户 = 还能干注册时那些事），how 要求念卡面时**两档都说全**。
    "set_account_role": (
        "会把那个账号的身份**换掉**（他当场被踢下线，重新登录后就是新身份："
        "换成杂鱼 = 他以后什么站内操作都做不了，只能闲聊；换成普通用户 = 他重新"
        "拥有一个普通访客的全部能力。两种都是**换档**，卡面上印着他现在的身份）",
        "请把**账号名以及「从哪个身份 → 到哪个身份」**都说全地念给主人看"
        "（卡面上还有它的 id 与当前身份，让他核对是不是那个人、这一下是收权还是还权），"
        "然后**请他明确回一句话要办**——**这一轮到这里就够了：不要替他换一个账号，也不要换个说法重试**"),
    # 禁言两件的 why 与冻结族**必须不同形**，差异点落在**后果**上而不是动词上——
    # 而这一族的后果恰好是冻结族的**反面**：禁言不踢人下线（Rust `set_user_muted`
    # 只写 `muted_until` 一列、不 bump `token_version`），他照常登录、浏览、跟 agent
    # 对话，只是**发不出评论与留言**。照着冻结族那两句抄过来（"会话全部失效 / 要重新
    # 登录"）就是一句假话，当事人会以为自己被封号了（「消息壳架空判据」那一族坑）。
    # 禁言方向多一格**期限**：主人点确定前唯一能发现"我让关三天、卡上却是永久"的地方。
    "account_mute": (
        "会让那个账号**发不出评论与留言**（他**照常登录、浏览文章、跟你对话**——"
        "禁言不动登录，卡面上印着他现在的状态与**这次要禁多久**）",
        "请把**账号名与这次禁言的期限**一字不改地念给主人看（卡面上还有它的 id 与"
        "当前状态，让他核对是不是那个人、关多久），然后**请他明确回一句话要办**——"
        "**这一轮到这里就够了：不要替他换一个账号，也不要换个说法重试**；"
        "**不要**说成「他会掉线 / 登录不了」"),
    "account_unmute": (
        "会解除那个账号的禁言（他**重新能发布评论与留言**；"
        "他的登录从来没有被影响过——禁言只挡发言）",
        "请把**账号名一字不改**地念给主人看（卡面上还有它的 id 与当前状态，"
        "让他核对是不是那个人），然后**请他明确回一句话要办**——**这一轮到这里就够了："
        "不要说成「放他回来 / 他的账号回来了」，也不要换个说法重试**"),
    # 发通知的 why 与前三族**必须不同形**，差异点同样落在**后果**上：公告族是"全站
    # 访客都会看到"，冻结族是"当场踢下线"，这一条是"**只给这一个人**看、但**发出去
    # 就收不回**（站内没有删除已发通知的功能）"。措辞里刻意**不写**「全站可见」
    # （那是公告，写成那样会让主人以为发错了影响面）。
    "send_user_notice": (
        "会把这段话**直接发到那个账号的个人中心**（**只有他一个人**看得到；"
        "但站内没有删除已发通知的功能——**发出后没有撤回的通道**）",
        "请把**收件账号名与通知正文全文**原样念给主人看（正文可以是你按他的意思"
        "整理过的那一版，但他必须看到**完整**的那一句——那是要署他名义发给别人的话），"
        "然后**请他明确回一句话要办**——**这一轮到这里就够了：不要再改正文，也不要换个说法重试**"),
    # 额度三件的 why **三条互不相同**，差异点同样只落在**后果**上（"会批准"这种
    # 同义反复等于没说）。三条各有一条是另两条没有的：批准=不可撤销；驳回=会通知
    # 他且额度不变；主动重置=**不需要他申请过**。
    # 驳回那条必须点明"他会收到一条通知"——那是这一下唯一对外可见的后果，而且
    # 理由会原样进那条通知（`docs/security-boundary.md §7⑫` 登记的文案）。
    "approve_quota_request": (
        "会把那个账号的**对话额度恢复到上限**（他马上能继续提问，等于再给他一整份额度；"
        "**这个动作不可撤销**——额度是算出来的，没有「撤回」这回事）",
        "请把**申请人的账号名一字不改**地念给主人看（卡面上还有他的 id、当前用量与"
        "**申请理由全文**，让他核对是不是该批的这个人），然后**请他明确回一句话要办**——"
        "**这一轮到这里就够了：不要替他换一个人，也不要换个说法重试**"),
    "reject_quota_request": (
        "**不会改变他的任何额度**（他还是用尽的样子，可以重新申请）；但会给他发一条"
        "**站内通知**，把你给的理由原样告诉他（站内没有删除已发通知的功能——"
        "**发出后没有撤回的通道**）",
        "请把**申请人的账号名与驳回理由**原样念给主人看（卡面上还有他的 id、当前用量"
        "与申请理由全文，让他核对是不是该驳的这个人），然后**请他明确回一句话要办**——"
        "**这一轮到这里就够了：理由必须是他说的那一句，不要自己编，也不要换个说法重试**"),
    "reset_user_quota": (
        "会把那个账号的**对话额度恢复到上限**（他马上能继续提问；**这个动作不可撤销**）。"
        "注意这一下**不需要他申请过**——是你替他决定的一次重置，他那边不会收到任何提示",
        "请把**账号名一字不改**地念给主人看（卡面上还有它的 id 与当前用量，"
        "让他核对是不是那个人），然后**请他明确回一句话要办**——**这一轮到这里就够了："
        "不要替他换一个账号，也不要换个说法重试**"),
    # 文章评论的两件（20261019）。两段 why 与留言那两件**同族但措辞不同**，差别全部
    # 来自"评论挂在某篇文章下"——那段话里必须**同时**说出"这是文章评论，不是河灯留言"
    # （说成留言，主人会去翻错的那个后台页；两族的差别见 `adminops_comment` 头注）。
    # 除此之外的纪律与留言逐条相同：复核可改判、评论本身不会消失；删除没有回收站。
    "audit_comment": (
        "会把那条**文章评论**（长在某篇文章正文下面的，不是河灯留言）**对全体访客"
        "放行显示**（通过）或**只对它自己可见**（驳回）——两种都是**可改判**的"
        "（驳回的能再放行），评论本身**不会消失**",
        "请把卡面上那条评论的**作者、正文与它挂在哪篇文章下**一字不改地念给主人看，"
        "并说清这一下是**放行还是驳回**，然后**请他明确回一句话要办**——"
        "**这一轮到这里就够了：不要改 verdict，也不要换个说法重试**"),
    "delete_comment": (
        "会删掉**访客写下的一条文章评论**（长在某篇文章正文下面的，不是河灯留言；"
        "删了公开侧立刻看不见，主人自己没有恢复入口）",
        "请把**那条评论的 #id、作者、正文与它挂在哪篇文章下**一字不改地写出来，"
        "并请他明确说一句命令——不要替他改写评论内容"),
    # 删标签（20261019 安全审计 SAU-014）。这一段的**全部重点在"成片 + 不可逆"**：
    # 卡面必须把连坐范围说全，因为它是主人点确定前唯一读得到的后果句，而这件事
    # 与"删一个分类"（`delete_category`，只把文章变成"没有分类"、可重建）在代码上
    # 长得几乎一样——只说"删掉一个标签"会让他按删分类的量级去判断。
    "delete_tag": (
        "会删掉一个**文章标签**——删**一级**标签会**连带删掉它下面的全部二级标签**，"
        "并把这些标签从**所有文章**上摘掉（文章本身不会被删）；站内没有"
        "「恢复标签」这个动作，删掉的标签与文章之间的关联**重建不回来**",
        "请把卡面上那个标签的**名字与层级**、以及**会一起被删掉的子标签名单**"
        "（有几个、分别叫什么）一字不改地念给主人看，再说明它原本挂在几篇文章上，"
        "然后**请他明确回一句话要办**——"
        "**这一轮到这里就够了：不要换个说法重试，也不要改用别的标签工具**"),
}


def consent_frame(tool: str, principal: Principal | None) -> str:
    """未获确认时的 __ERROR__ 帧文本。

    形态与权限拒绝同族（同为**确定性拒绝**、同走 blocked 链路），但语义不同：
    这不是"身份不允许"，而是"这事还没得到用户同意"——所以文案要求它**去问**，
    而不是宣称做不到。用 __ERROR__ 而不是普通文本是有意的：gate 的分支 5a
    （错误帧 + 完成式声称 → fallback）因此自动生效，**叙述侧无法把它说成"已发布"**。

    文案取两处（20260925）：工具族覆盖（`_CONSENT_WHY_TOOL`，一律弹窗族那一批——
    它们不是"改哪一篇"）优先，否则按 scope 取。
    """
    who = f"uid={principal.uid} role={(principal.role if principal else None) or '未知'}"
    scope = required_scope(tool) or ""
    why, ask = _CONSENT_WHY_TOOL.get(tool) or _CONSENT_WHY.get(
        scope, ("会改动站点上的数据", "请先向用户确认这一次要不要做"))
    return (f"__ERROR__: 待确认[{REASON_CONSENT}] —— {who} 请求的写操作 {tool} "
            f"{why}，而**用户本轮消息里没有明确确认**。"
            f"本轮未执行、也不得声称已完成：{ask}。")


_CONSENT_ERR_RE = re.compile(r"待确认\[([a-z_]+)\]")


def consent_error_reason(text: str) -> str | None:
    """从 __ERROR__ 帧文本里取回确认拒绝的原因码（checker 判 reason 用）。"""
    m = _CONSENT_ERR_RE.search(text or "")
    return m.group(1) if m else None


# ── 同意闸挡下**一件写**时的两样产出（20261016，§1.57 候选改法①）──────────────
# 反极性改选（主人说「收藏」、planner 排的是「取消收藏」）走的是**同一个**同意闸出口：
# 那一件同样"本轮消息里没获确认"（`_own_command` 判淡），所以它本来就会被 `consent_missing`
# 接住——只不过报的是 `consent_required`，于是卡上问的还是那个错的方向。
#
# 为什么是**两个**函数而不是一个返回 `(码, 文)` 的：唯一的消费方 `graph.execute_node`
# 的行数/复杂度预算**一格都不剩**（`tests/test_complexity_budget.py`，台账 400 行 / C=78
# 且**只许缩小**），一次元组解包就要多一行。所以分成"一处要码、一处要文"两个薄函数。
# 它们**不各自判一遍**——都只问同一个谓词 `action_conflict`，判据只有一份
# （`tests/test_own_action_conflict.py` 用源码锁钉住这一点）。两处必然同进同出：
# 同一个 `user_msg`、同一个 `name`、同一个纯函数。
def consent_block_reason(user_msg: str | None, tool: str | None) -> str:
    """同意闸挡下这一件写时的**原因码**：反极性改选 → `action_conflict`，否则 `consent_required`。"""
    return REASON_ACTION_CONFLICT if action_conflict(user_msg, tool) else REASON_CONSENT


def consent_block_frame(tool: str, principal: Principal | None,
                        user_msg: str | None) -> str:
    """同一格的**帧文**。`action_conflict` 为真时改说"你排反了"（否则模型只会收到
    一句"要不要做"——那正是它已经答错的那个问题）。"""
    if action_conflict(user_msg, tool):
        return action_conflict_frame(tool, own_antipode(tool))
    return consent_frame(tool, principal)
