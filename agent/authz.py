"""权限模型（scope manifest）——"秘书能做什么"的唯一事实来源（20260920）。

设计取向（与全仓一致）：**能力用声明表达，判据在一个确定性点上**。
  工具清单是 `tools/base.py::_TOOL_REGISTRY`（业务唯一数据源），这里给它配一张
  「工具 → 所需 scope」的声明表；角色 → scope 的授予表也在这里。运行时唯一的
  判据点是 `check(principal, tool)`，由 graph.execute_node 在**调用工具之前**执行
  （与断连检查、参数引用解析同一层：确定性、无 LLM、无一例外）。

现在**默认不拦**（shadow 模式，`AGENT_AUTHZ_ENFORCE=0`）：
  决策照算、照进 trace，但不改变行为。这是为秘书功能做的前置测绘——真实流量里
  跑一段，看"谁在什么时候被拒"，用证据校准授予表，再打开开关。理由同
  `agent_require_assertion` 的滚动上线：先观测、后收口，别拿在途请求做实验。

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

from agent.principal import KNOWN_ROLES, ROLE_ADMIN, ROLE_SECRETARY, ROLE_USER, Principal

# ── scope 词汇表 ─────────────────────────────────────────────────────
# 命名 = <动作>.<对象>。对象轴现在只有「谁的」这一维（public / own / any），
# 第二维（哪类资源）等真有第二个消费者再加——先够表达"秘书比访客多什么"。
SCOPE_READ_PUBLIC = "read.public"      # 公开内容：文章/标签/分类/留言/说说/公告/站点信息
SCOPE_READ_OWN = "read.own"            # 自己的私有数据：自己的会话历史、自己的设备
SCOPE_READ_ANY = "read.any"            # 他人的私有数据（秘书的核心增量）
SCOPE_WRITE_PAGE = "write.page"        # 作用于访客自己看到的页面：导航/特效/夜间模式
SCOPE_WRITE_DEVICE = "write.device"    # 物理世界写操作：IoT 设备（当前唯一：屏幕刷字）
SCOPE_WRITE_CONTENT = "write.content"  # 代用户写站点内容（留言/说说/文章）——尚未有工具
SCOPE_ADMIN_CONSOLE = "admin.console"  # 后台管理面**读**（Rust auth_guard 后面的东西）
SCOPE_WRITE_CONSOLE = "write.console"  # 后台管理面**写**（标签/文章状态——20260921 第二轮新增）
# 用户**自己**的私有数据写（20260923 第七轮：收藏文章、标记通知已读）。
# 与 write.page 的区别：页面/特效写的效果就在用户眼前那一屏上（改错了立刻看得见也
# 立刻能改回来），而收藏与已读是**落库的私有状态**（刷新还在）；与 write.content /
# write.console 的区别：它只动**自己的**私有数据——不外显、不碰别人的东西，
# 所以不需要后者那两道（`_HARD_SCOPES` 硬拦、`_ALWAYS_CONFIRM_TOOLS` 一律弹窗）。
SCOPE_WRITE_OWN = "write.own"

ALL_SCOPES = frozenset({
    SCOPE_READ_PUBLIC, SCOPE_READ_OWN, SCOPE_READ_ANY,
    SCOPE_WRITE_PAGE, SCOPE_WRITE_DEVICE, SCOPE_WRITE_CONTENT, SCOPE_WRITE_OWN,
    SCOPE_ADMIN_CONSOLE, SCOPE_WRITE_CONSOLE,
})

# 写操作：留给后续"人在回路确认"挂钩（见 docs/secretary.md 的前置需求 ③）
WRITE_SCOPES = frozenset({SCOPE_WRITE_PAGE, SCOPE_WRITE_DEVICE, SCOPE_WRITE_CONTENT,
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
_HARD_SCOPES = frozenset({SCOPE_ADMIN_CONSOLE, SCOPE_WRITE_CONSOLE})
# 20260923 刻意**没有** `write.own`：那张表的判据是"纯新增能力、历史流量里一条都没有、
# 没有观测期可谈"，而"给自己收藏一篇文章"恰恰是**每个登录用户本来就能在页面上做的事**
# （收藏按钮、个人中心），工具只是换了只手来做 ⇒ 它满足观测期的前提，进 shadow 是正当的。
# 方向也相反：放行错了的代价是"多了一条自己的收藏"（自己看得见、一键能取消），
# 而不是"网站对外可见状态被改了"。`write.own` 的写面守卫靠下面三道（都确定性）：
#   ① scope 本身（三个角色都授予，但没有身份就没有 uid，工具层 uid<=0 直接不发请求）
#   ② 同意闸（判"本轮有没有明确命令"，见 `_own_command`）
#   ③ 工具层的写后复核（复核不出那一行就报 unavailable，不谎称成功）

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
    ROLE_SECRETARY: frozenset({
        SCOPE_READ_PUBLIC, SCOPE_READ_OWN, SCOPE_READ_ANY,
        SCOPE_WRITE_PAGE, SCOPE_WRITE_DEVICE, SCOPE_WRITE_CONTENT, SCOPE_WRITE_OWN,
    }),
    ROLE_ADMIN: ALL_SCOPES,
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
    # 用户自己的数据·**写**那一半（20260923 批 7）：收藏 / 取消收藏 / 标记已读。
    # 写的是同一个人的同一份数据，所以 scope 是 read.own 的写方向 `write.own`：
    # 三档角色都有、匿名没有、**不进 `_HARD_SCOPES`**（秘书代博主收藏是正当的
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
    # 以及本机的服务/磁盘/日志。scope 取 admin.console 而不是 read.any：
    # 秘书可以读"他人数据"，但读**运维面**是博主本人的事（见 docs/secretary.md）。
    "get_server_status": SCOPE_ADMIN_CONSOLE,
    "get_service_health": SCOPE_ADMIN_CONSOLE,
    "get_moderation_status": SCOPE_ADMIN_CONSOLE,
    "get_user_stats": SCOPE_ADMIN_CONSOLE,
    # 管理助手（20260921 第二轮）：后台**写**。<动作>.<对象> 与 admin.console
    # 成对：一个是这道门的读方向，一个是写方向。
    #   `list_admin_notes` 取 admin.console 而不是 read.any——它读的是**后台**
    #   文章列表（含草稿/私密），与上面四个报表工具同一个门；没有它，"把草稿
    #   发布出来"这条指令在 planner 侧拿不到 id（公开接口一律滤 is_public）。
    #   三个写工具取 write.console：进 _HARD_SCOPES（不吃 shadow）+ 进
    #   CONSENT_SCOPES（每次要命令式确认）。secretary 刻意不给——后台写与
    #   admin.console 同域，Rust 那道门也只认 admin。
    "list_admin_notes": SCOPE_ADMIN_CONSOLE,
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
    # 第九轮（20260926）：冻结 / 解冻后台账号。同门（write.console）——改的是
    # **第三方的登录能力**（被冻的人当场被踢下线，且解冻也换不回他那批会话），
    # 后端那两条路由同样挂在 `auth_guard` 后面（见 src/routes/temp_user.rs）。
    "freeze_account": SCOPE_WRITE_CONSOLE,
    "unfreeze_account": SCOPE_WRITE_CONSOLE,
}


# ── 写操作的「人在回路」确认（20260920，秘书类功能前置需求 ③）────────────
# 分工：**权限**回答"这个人能不能做"，**确认**回答"这一次他到底要不要做"。
# 只有**离开用户自己眼前**的写入才需要确认：写站点内容（留言/说说/文章）发出去
# 就收不回、且以用户名义对他人可见，而页面/设备写操作的效果就发生在用户眼前
# （他立刻看得见、也立刻能改回来），既有行为不动它。
#
# 20260921 第二轮把 `write.console` 加进来：后台写改的是**对外可见状态**
# （一篇文章从公开变私密，读者立刻打不开），且改完不会自动复原。
# 20260923 第三位成员 `write.own`：它**不满足**上面那条"离开用户眼前"的措辞（收藏与
# 已读都只在自己账号里，不外显、可逆），但满足那条措辞背后的**真判据**——"这一次到底
# 要不要做"需要一个确定性答案。反过来说：agent 自己判断"这篇值得收藏"就替用户收藏了，
# 是**以用户的名义往他账号里写状态**，与"写站点内容"同一类错（只是影响面小）。所以
# 仍要一句明确的命令；判不出来时走既有弹窗（`graph._confirm_popup`），不是硬拒。
CONSENT_SCOPES = frozenset({SCOPE_WRITE_CONTENT, SCOPE_WRITE_CONSOLE, SCOPE_WRITE_OWN})

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
    # 而且删了没有回收站（`delete_board_comment`）。审核（通过/驳回）**不在这张
    # 表**：它可改判（驳回的能再放行），且改的只是可见性——与 `set_article_status`
    # 同类，走既有的"命令式措辞才免问"那条路（判不出来就弹窗，fail-closed 方向不变）。
    "delete_board_comment",
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
})

# 每个需确认的 scope 配一张**确认语表**：用户的**本轮消息**命中才算确认。
# 刻意收窄（"确认发布"这种明确说法）——fail-open 的代价是未经同意把内容发出去，
# 宁可多问一轮。扩表时先问一句：这句话会不会被误读成确认？
#
# 表值可以是 `re.Pattern`（`.search`）或**谓词 `(msg) -> bool`**（20260921 加）：
# write.console 的判据要同时管"有没有命令骨架"和"是不是在提问/假设"，写成一条
# 正则既读不懂也测不动——拆成几个具名小判据，在 `_console_command` 里组合。
_CONSENT_PATTERNS: dict[str, object] = {
    SCOPE_WRITE_CONTENT: re.compile(
        r"(确认|同意|批准|就这么)(发布|发送|提交|发出去|发|写)"
        r"|确认(就)?这样(发|写)|授权(发布|发送|提交)"),
    SCOPE_WRITE_CONSOLE: None,  # 占位，见下方 _console_command 注册
}


# ── write.console 的「命令式判据」（20260921）─────────────────────────
# **它判的是"本轮有没有明确命令"，不是"第二次确认"**（用户拍板：同轮命令即确认
# ——管理员说「把《架构文档》设为私密」本身就是命令，再要一句"确认"是把确认闸
# 做成复读机）。三个小判据按 AND 组合，每一条都能单独写正反用例：
#
#   ① 有明确目标（id / 《书名号》/ 指代词 / 标签名）
#   ② 有后台写动作词
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
# 命令骨架：句首的「把/将/给」，或句首的动词，或「请/帮我…」起首。
_CONSOLE_ORDER_RE = re.compile(
    r"(?:^|[。！!；;\n，,])\s*(?:请|帮我|帮忙|麻烦|记得|快去)?\s*(?:把|将|给)"
    r"|^\s*(?:请|帮我|帮忙|麻烦|记得)\s*\S"
    r"|^\s*(?:新建|创建|建立|添加|发布|置顶|取消置顶|取消顶置|隐藏|公开|私密|下架)"
    # 「取消…置顶」把动词拆开了（宾语插在中间）："取消文章 12 的置顶"是**最常见的
    # 撤顶说法**，上面第三支的"取消置顶"连写认不到。只放 置顶/顶置 两个宾语——
    # 「取消…标签」那类不放：`功能/排序` 之类的抽象宾语会与闲聊撞车，而"取消标签"
    # 另有"把文章 12 的标签去掉/去掉标签"等**动词连写**的说法可走（fail-closed 的
    # 方向是多问一句，不是多写一次生产数据）。
    r"|^\s*(?:取消|解除)[^\n。！？!?；;，,]{0,12}?(?:置顶|顶置)")
# 「确认…」命令骨架（20260921）：**agent 自己建议、用户照抄**的那种短回声
# （"确认创建标签 X"）。生产实测里这句被判 False ⇒ 同意闸不放行 ⇒ 死路一条
# （详见 20260921 事故：用户照着建议打了一遍，系统还说不算命令）。
#
# 为什么单独成条正则而不是并进上面那张表：这条**只认句首**（回声必然是整句），
# 且要配下面的疑问尾排除——两条判据的组合语义（"像回声" ∧ "不是在打听"）
# 用一条正则写不出来，分开写能各自测。
_CONSOLE_CONFIRM_ORDER_RE = re.compile(
    # ① 把字结构：「确认把文章 12 设为私密」——动词在宾语之后，中间要允许宾语
    r"^\s*确认(?:一下)?\s*(?:把|将|给)[^\n。！？!?；;，,]{1,20}?"
    r"(?:设为|设成|改成|置顶|取消置顶|隐藏|发布|下架|打上|加上|去掉|移除)"
    # ② 动词直接跟在确认之后：「确认创建标签 X」「确认置顶文章 12」
    r"|^\s*确认(?:一下)?\s*"
    r"(?:创建|新建|建立|新增|建|加|打上|加上|设为|设成|改成|置顶|取消置顶|隐藏|发布|下架)")
# 疑问尾：出现即说明这是**在打听**（多久/多少钱/怎么弄），不是在下命令。
# 收窄的理由和上面一样——一个误判的代价是一次真写，而多问一次的代价只是弹个窗。
_CONSOLE_INQUIRY_TAIL_RE = re.compile(
    r"多少|多久|几天|费用|价格|流程|步骤|怎么|怎样|如何|什么样|能不能|可不可以|"
    r"需要|要不要|吗|呢")


# 动作词（后台写域；"公开/私密/草稿/发布"这些裸词靠"命令骨架 + 目标"两项兜住，
# 单看它们会与陈述句撞车）。
#
# 20260922 补「改名 / 改名叫」：活体探针腿⑭ 实测，「把分类「X」改名叫「Y」」这句
# 教科书式命令**命不中快道**（表里只有「改名为」「改成」），于是落进弹窗那条路——
# 弹窗本身是更安全的 fail-closed 取向、不是缺陷，但用户明明下的是命令，却被多问
# 一次；而词表的语义是"哪些说法算明确的命令"，这里缺的正是最常用的那个说法。
# （"改名"是"改名叫/改名为"的子串，单列只为可读性；`_console_target` 靠同一张表
# 判"这个词是不是标签名"，多几项只会让它更保守。）
_CONSOLE_VERBS = (
    "设为私密", "设为公开", "设为草稿", "设成私密", "设成公开", "设成草稿",
    "改成私密", "改成公开", "改成草稿", "转成私密", "转成公开", "转为私密", "转为公开",
    "置顶", "取消置顶", "取消顶置", "顶置", "隐藏", "公开", "私密", "草稿",
    "发布", "下架", "撤下",
    "新建", "创建", "建立", "新增", "建一个", "建个", "添加", "打上", "加上", "打个",
    "改成", "改为", "换成", "改名为", "改名", "改名叫", "取消标签", "去掉标签", "移除标签", "删掉标签",
    "去掉", "移除",
)
# 「标签 / 叫 <名字>」形态的目标：名字取到空白或标点为止。
_CONSOLE_TAG_NAME_RE = re.compile(
    r"(?:叫|名为|叫做|名字是|名称是|标签)\s*[：:]?\s*[\"'“”‘’]?"
    r"([^\s，。！？!?；;\"'“”]{1,30})")


def _console_target(text: str) -> bool:
    """消息里有没有**明确的目标**（数字 id / 《书名号》/ 指代词 / 一个标签名）。

    标签名那一支要做一次反查：「把**标签去掉**」里"标签"后面跟的是**动作词**，
    不是名字——只看正则的话它和「新建标签 Python」长得一模一样，会把一
    「把标签去掉」判成有目标的命令。所以命中后要求那个"名字"不落在动作词表里。
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
    return not any(v.startswith(name) or name.startswith(v) for v in _CONSOLE_VERBS)


def _console_confirm_order(text: str) -> bool:
    """「确认 + 写动词」的**短回声**骨架（见 _CONSOLE_CONFIRM_ORDER_RE 注释）。"""
    if not _CONSOLE_CONFIRM_ORDER_RE.search(text):
        return False
    return not _CONSOLE_INQUIRY_TAIL_RE.search(text)


# 系统注入的消息壳（server.py:413 给本轮用户消息加的 `[当前问题]: ` 锚点）。
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


def _console_command(msg: str, tool: str | None = None) -> bool:
    """本轮消息是不是一条明确的后台写命令（确定性、无 LLM）。

    `tool` 收下但**不用**：后台写的判据只回答"是不是命令"，落到哪个工具由技能模板与
    `_ARTICLE_WRITE_TOOLS` 的目标校验管（那里比"这句话里有没有这个动作词"更硬）。
    签名统一成两参只是为了 `consent_granted` 的那一处调用（见它自己的注释）。
    """
    text = _strip_system_tags((msg or "").strip())
    if not text:
        return False
    if _CONSOLE_QUESTION_RE.search(text) or _CONSOLE_HYPOTHESIS_RE.search(text):
        return False
    if not _console_target(text):
        return False
    if not any(v in text for v in _CONSOLE_VERBS):
        return False
    return bool(_CONSOLE_ORDER_RE.search(text)) or _console_confirm_order(text)


def is_question_like(msg: str) -> bool:
    """这句是**提问/假设**（而不是一个意图陈述）吗？

    弹窗分叉用它（graph.py::execute_node）：判不出来是"用户有意向但没判成命令"
    → 弹窗问一次；判出来是提问/假设 → 绝不能弹（用户只是在问，弹一个"确定/取消"
    等于把提问读成了意图）。空消息保守按提问走（无从判断时不弹）。
    """
    text = _strip_system_tags((msg or "").strip())
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
    # ⚠️ 这一行必须是 **rf** 串：`{{0,3}}` 在 rf 里才折叠成量词 `{0,3}`，写成普通 r 串
    # 就变成"字面量 {0,3}"（永远匹配不上）⇒ 整支动词起首分支恒不命中（本批改这句时
    # 真踩过一次：只有「把…」那支还能过）。
    rf"(?:(?:所有|全部|全|都|未读的?|未读通知|通知|公告|站内信|私信|信件|消息)\s*){{0,3}}"
    r"(?:都\s*)?" + _OWN_READ_VERB + r"[^\n。！？!?；;，,]{0,6}?已读"
)

_OWN_FAMILY_RE: dict[str, re.Pattern] = {
    _OWN_FAMILY_ADD: _OWN_ADD_RE,
    _OWN_FAMILY_REMOVE: _OWN_REMOVE_RE,
    _OWN_FAMILY_READ: _OWN_READ_RE,
}

# 量词型打听（「我收藏了**哪些**文章」「还有**几条**没读」）。上面两张共享表都不含
# "哪些/几个"这类**问数量的词**（它们是给后台写用的，那里的问题词都由 吗/怎么 兜住）。
_OWN_INQUIRY_RE = re.compile(r"哪些|哪个|哪篇|哪条|几篇|几条|几件|几个|几次")


def _own_command(msg: str, tool: str | None = None) -> bool:
    """本轮消息是不是一条明确的「写自己私有数据」命令（确定性、无 LLM）。

    `tool` 是**必需**的（见上面那段注释：判的是"命令**这一个**工具"）。未登记家族的
    工具 → False（fail-closed）。
    """
    family = _OWN_TOOL_FAMILY.get(tool or "")
    if family is None:
        return False
    text = _strip_system_tags((msg or "").strip())
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

    只看 scope 是否在 CONSENT_SCOPES ——**声明驱动**，所以将来新增一个
    write.content 工具会自动落在闸下，不需要有人记得来改这个函数。
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


def enforcing(scope: str | None = None) -> bool:
    """这个 scope 是否真的拦（默认 False = shadow：只算不拦）。

    `scope=None` = 沿用旧的"全局开关"语义（不知道 scope 的调用点用它）。
    传了 scope 且它在 `_HARD_SCOPES` 里 → 恒 True（见该常量的注释）。
    """
    if scope in _HARD_SCOPES:
        return True
    from config.settings import settings
    return bool(getattr(settings, "authz_enforce", False))


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
    SCOPE_WRITE_CONTENT: (
        "会把内容发布到站点上（对外可见、收不回）",
        "请把要发布的内容原样告诉用户，并请他明确回复确认（例如「确认发布」）"),
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
_CONSENT_WHY_TOOL = {
    "create_announcement": (
        "会发布一条**全站访客都会看到**的公告（首页可见，删掉不会自动回来）",
        "请把**标题与正文原文**一字不改地念给主人看（这是要署他名义对全体访客说的话，"
        "一个字都不要改、也不要替他润色），然后等他点确认——"
        "**这一轮到这里就够了：不要再改内容，也不要换个写法重试**"),
    "update_announcement": (
        "会改动一条**全站访客都会看到**的公告（首页可见）",
        "请把**改后的标题与正文原文**一字不改地念给主人看，然后等他点确认——"
        "**不要再改内容，也不要换个写法重试**"),
    "delete_announcement": (
        "会删掉一条**全站访客都会看到**的公告（删了没有回收站）",
        "请把那一条公告的**标题与正文原文**一字不改地念给主人看，"
        "并请他明确说一句命令——不要再换个写法重试"),
    "delete_board_comment": (
        "会删掉**访客写下的一条留言**（删了没有回收站）",
        "请把**那条留言的 #id、作者与原文**一字不改地写出来，"
        "并请他明确说一句命令——不要替他改写留言内容"),
    "create_dashboard_todo": (
        "会往主人**后台首页的待办列表**里加一条（那是他自己那份列表，加完可改可删）",
        "请把**这条待办的正文与排期日**一字不改地念给主人看（排期按「X月X日」说，"
        "他没说日期就说「未排期」），然后等他点确认——"
        "**这一轮到这里就够了：不要再改写正文，也不要换个说法重试**"),
    # 冻结与解冻的 why **必须不同形**，而且差异点要落在**后果**上而不是动词上
    # （「会冻结」/「会解冻」等于没说）：一个说的是"会话全没了且解冻也换不回来"，
    # 一个说的是"他这才重新能登录"。两条都**不许**承诺"恢复原状"——`token_version`
    # 只增不减，解冻恢复的只是"能不能登录"（`scripts/probe_token_revoke.py` 的【三】）。
    "freeze_account": (
        "会把那个账号**当场踢下线**（他所有已登录的会话立刻失效，且解冻也换不回"
        "那批会话，他得重新登录）",
        "请把**账号名一字不改**地念给主人看（卡面上还有它的 id 与当前状态，"
        "让他核对是不是那个人），然后等他点确认——**这一轮到这里就够了："
        "不要替他换一个账号，也不要换个说法重试**"),
    "unfreeze_account": (
        "会让那个账号**重新能登录**（他此前被踢下线的会话不会自动回来，"
        "要他本人重新登录一次）",
        "请把**账号名一字不改**地念给主人看（卡面上还有它的 id 与当前状态，"
        "让他核对是不是那个人），然后等他点确认——**这一轮到这里就够了："
        "不要说成「恢复原状」，也不要换个说法重试**"),
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
