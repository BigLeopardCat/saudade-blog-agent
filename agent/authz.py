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
  - 未声明的工具 → **拒绝**（fail-closed），并由 test_authz.py 的完备性断言在
    CI 层拦住——新增工具忘了声明是工程疏漏，不该靠运行时宽容。
  - 拒绝的形态复用既有 blocked 链路（`__ERROR__` 帧 + `scope_denied` 原因码）：
    planner 如实收尾、reflector 受限复盘，不新增决策分支，也不静默吞掉。

跨语言契约：角色名与 scope 名与 Rust 侧 `src/authz.rs` 必须一致（roles 是
`user.role` 列的取值域，scope 是两边的共同词汇表）。改一侧须同步另一侧 +
两侧各自的单测（agent: test_authz.py / rust: authz.rs 尾部 #[cfg(test)]）。
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

ALL_SCOPES = frozenset({
    SCOPE_READ_PUBLIC, SCOPE_READ_OWN, SCOPE_READ_ANY,
    SCOPE_WRITE_PAGE, SCOPE_WRITE_DEVICE, SCOPE_WRITE_CONTENT,
    SCOPE_ADMIN_CONSOLE, SCOPE_WRITE_CONSOLE,
})

# 写操作：留给后续"人在回路确认"挂钩（见 docs/secretary.md 的前置需求 ③）
WRITE_SCOPES = frozenset({SCOPE_WRITE_PAGE, SCOPE_WRITE_DEVICE, SCOPE_WRITE_CONTENT,
                          SCOPE_WRITE_CONSOLE})

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
# "有确认语的非管理员能把文章设成私密"。test_admin_write.py 有专门一条断言锁它。
_HARD_SCOPES = frozenset({SCOPE_ADMIN_CONSOLE, SCOPE_WRITE_CONSOLE})

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
_ROLE_SCOPES: dict[str, frozenset[str]] = {
    ROLE_USER: frozenset({
        SCOPE_READ_PUBLIC, SCOPE_READ_OWN, SCOPE_WRITE_PAGE, SCOPE_WRITE_DEVICE,
    }),
    ROLE_SECRETARY: frozenset({
        SCOPE_READ_PUBLIC, SCOPE_READ_OWN, SCOPE_READ_ANY,
        SCOPE_WRITE_PAGE, SCOPE_WRITE_DEVICE, SCOPE_WRITE_CONTENT,
    }),
    ROLE_ADMIN: ALL_SCOPES,
}

# ── 工具 → 所需 scope（**完备性是硬要求**：见 test_authz.py）───────────
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
}


# ── 写操作的「人在回路」确认（20260920，秘书类功能前置需求 ③）────────────
# 分工：**权限**回答"这个人能不能做"，**确认**回答"这一次他到底要不要做"。
# 只有**离开用户自己眼前**的写入才需要确认：写站点内容（留言/说说/文章）发出去
# 就收不回、且以用户名义对他人可见，而页面/设备写操作的效果就发生在用户眼前
# （他立刻看得见、也立刻能改回来），既有行为不动它。
#
# 20260921 第二轮把 `write.console` 加进来：后台写改的是**对外可见状态**
# （一篇文章从公开变私密，读者立刻打不开），且改完不会自动复原。
CONSENT_SCOPES = frozenset({SCOPE_WRITE_CONTENT, SCOPE_WRITE_CONSOLE})

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
_CONSOLE_VERBS = (
    "设为私密", "设为公开", "设为草稿", "设成私密", "设成公开", "设成草稿",
    "改成私密", "改成公开", "改成草稿", "转成私密", "转成公开", "转为私密", "转为公开",
    "置顶", "取消置顶", "取消顶置", "顶置", "隐藏", "公开", "私密", "草稿",
    "发布", "下架", "撤下",
    "新建", "创建", "建立", "新增", "建一个", "建个", "添加", "打上", "加上", "打个",
    "改成", "改为", "换成", "改名为", "取消标签", "去掉标签", "移除标签", "删掉标签",
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


def _console_command(msg: str) -> bool:
    """本轮消息是不是一条明确的后台写命令（确定性、无 LLM）。"""
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
# 措辞刻意贴着实词走（是什么/步骤/流程/…），不用"什么"这种宽词——宽词会把
# 「新建个标签，名字叫什么好」这类**真意图**也判成提问，那又回到死路。
_CONSOLE_INQUIRY_RE = re.compile(
    r"是什么|是啥|有什么|有多少|多少|多久|几天|步骤|流程|条件|要求|"
    r"影响|后果|风险|注意|区别|好处|坏处|可以吗|行吗")


_CONSENT_PATTERNS[SCOPE_WRITE_CONSOLE] = _console_command


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
    """
    spec = _CONSENT_PATTERNS.get(required_scope(tool) or "")
    if spec is None:
        return False
    msg = user_msg or ""
    if callable(spec):
        return bool(spec(msg))
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
}


def consent_frame(tool: str, principal: Principal | None) -> str:
    """未获确认时的 __ERROR__ 帧文本。

    形态与权限拒绝同族（同为**确定性拒绝**、同走 blocked 链路），但语义不同：
    这不是"身份不允许"，而是"这事还没得到用户同意"——所以文案要求它**去问**，
    而不是宣称做不到。用 __ERROR__ 而不是普通文本是有意的：gate 的分支 5a
    （错误帧 + 完成式声称 → fallback）因此自动生效，**叙述侧无法把它说成"已发布"**。
    """
    who = f"uid={principal.uid} role={(principal.role if principal else None) or '未知'}"
    scope = required_scope(tool) or ""
    why, ask = _CONSENT_WHY.get(
        scope, ("会改动站点上的数据", "请先向用户确认这一次要不要做"))
    return (f"__ERROR__: 待确认[{REASON_CONSENT}] —— {who} 请求的写操作 {tool} "
            f"{why}，而**用户本轮消息里没有明确确认**。"
            f"本轮未执行、也不得声称已完成：{ask}。")


_CONSENT_ERR_RE = re.compile(r"待确认\[([a-z_]+)\]")


def consent_error_reason(text: str) -> str | None:
    """从 __ERROR__ 帧文本里取回确认拒绝的原因码（checker 判 reason 用）。"""
    m = _CONSENT_ERR_RE.search(text or "")
    return m.group(1) if m else None
