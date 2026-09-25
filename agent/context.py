"""上下文组装（20260912 从 graph.py 拆出）——纯函数、零 LLM、零 LangGraph 依赖。

内容 = 注入给 planner/model 的事实文本生成：消息文本提取（多模态兼容）、页面
上下文 page_ctx、页面操作指南（GUESTBOOK_GUIDE / SITE_GUIDE）、工具帧摘要
（_frame_texts）、checker 回执摘要（_receipts_text）。

拆出动机：graph.py 曾是 2000+ 行单体——图拓扑（节点/边/路由）与该层纯文本
组装混在一起，读一处要翻半屏无关代码。本层被 graph 的节点调用，自身不依赖
图（decisions.py 也用它），是天然的叶子层。
"""

import ast
import json
import re

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from agent import sections
from agent.authz import strip_system_tags  # 消息壳剥除（见下方 _short_reply_kind 注释）

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
    "页面左下方有「此心为灯」留言入口，按流程选完河灯和印章后出现输入框（提示语「此刻想说的话…」），在框里写好内容即可放灯；"
    "留名框在输入框旁，默认预填当前登录账号昵称，清空留名或点「匿名」则以无名/"
    "匿名身份放灯；不需要注册或邮箱，输入框一直可见。"
    "放灯后页面顶部「我的河灯」页签可查看自己放过的灯。"
    "注意：本页面没有「昵称+邮箱+提交」式表单，需要先登录才能留言。"
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
_SITE_GUIDE_HEAD = (
    "【站内板块与技能清单】（系统注入的事实——介绍「博客有哪些板块/功能」或"
    "「你能做什么」时以此为准完整转述）站内板块：首页；文章（/article/<id> 单篇）；"
    "留言板=「河灯集」（/guestbook）；说说（/talk）；归档/时间轴（/times）；"
    "关于我（/about）；物联网平台控制台（/device-console/）；登录（/login）与"
    "后台管理（/dashboard）仅博主使用。"
)
# 非技能类的能力（多模态看图等）：注册表里没有对应技能，只能手写；保持极短，
# 真正会漂移的是"哪些技能可用"，那部分由注册表渲染（见 site_guide）。
_SITE_GUIDE_TAIL = "看访客发来的图片并描述内容/颜色。"
# 管理能力的引导语（能力**内容**仍来自注册表的 capability 字段，这里只有抬头）
_ADMIN_GUIDE_HEAD = "🔑 以管理员身份（本轮对话者是博主本人）你还额外能做："


def site_guide(role: str | None = None) -> str:
    """站内板块 + 能力清单（**由技能注册表按角色渲染**，20260921）。

    此前这份清单是手写死文本：注册表加了管理助手三件写、清单里一个字没变 ⇒
    narrator 一会儿说"我可以建标签"、一会儿说"我不能改后台"（165525/165544 vs
    165645/165937，同一能力三轮两种答案），而它**说的每一句都"有据"**——据的是
    那份不会动的清单。现在能力行 = 注册表的 capability 字段 × 同一套角色可见性
    判据（skills.visible_skills），加技能/改角色只动注册表一处。

    可见性是**分流不是隐藏**：访客看不到管理能力行（他不该被告知"我能改后台"），
    管理员看到的清单里明确列出他能改什么（他不该被告知"我不能改"）。
    """
    from agent.skills import visible_skills        # 局部导入：context 是叶子层，
    base, admin = [], []                           # skills 反过来不依赖 context，但
    for s in visible_skills(role):                 # 保持此处零模块级耦合更稳
        if not s.capability:
            continue
        (admin if s.roles else base).append(s.capability)
    caps = "；".join(base + [_SITE_GUIDE_TAIL])
    out = _SITE_GUIDE_HEAD + f"你能做的：{caps}"
    if admin:
        out += _ADMIN_GUIDE_HEAD + "；".join(admin) + "。"
    return out + "介绍能力时按此完整列出，不要遗漏。"


# 无角色渲染（模块级常量）：既有导入点与 test_skills 的板块覆盖断言仍以此为准。
# **不要**在运行时用它——运行时要按角色取 site_guide(role)，否则管理员能力行
# 又会漏掉（这正是本轮修的那个洞）。
SITE_GUIDE = site_guide(None)


def _attach_page_guide(page_ctx: str, role: str | None = None) -> str:
    """页面上下文常驻附板块/技能清单；命中留言板时再附操作指南（URL 是系统
    上报事实，非模型推断；两份指南同为"只转述"系统数据）。

    `role` 决定能力清单里**列不列管理能力**（site_guide 按角色渲染）——这是
    20260921 的洞：模板固定 ⇒ 管理员轮里清单不含写能力 ⇒ narrator 照单说
    "我不能改后台"（165525/165544），而它讲的是系统注入的事实。
    """
    try:
        out = (page_ctx or "") + "\n" + site_guide(role)
        if _GUESTBOOK_URL_RE.search(page_ctx or ""):
            out += "\n" + GUESTBOOK_GUIDE
        return out
    except Exception:
        return page_ctx or ""


def _page_ctx(messages: list, role: str | None = None) -> str:
    """提取前端实时上报的页面上下文（page/title/特效/夜间），注入 planner/model。

    前端每轮请求都携带真实 current_url（window.location.href），_build_messages
    写入首条 [System: ...] 消息。planner/model 若只凭对话推断访客位置会脱节：
    用户手动转跳后对话历史不体现页面变化（曾见用户说"已经离开物联网控制台了，
    在首页"，模型仍延续上一轮的设备显示动作）。此处显式提取注入 prompt——
    事实以系统上报为准，不依赖模型推断。

    `role` 透传给 _attach_page_guide（决定能力清单是否含管理能力）。缺省 None =
    访客口径：**调用方默认应当把角色传进来**，漏传只会少列管理能力而不会多列
    （fail-closed 方向正确）。
    """
    for m in messages:
        content = _msg_text(m) or ""
        found = re.search(r"\[System:\s*(.*?)\]", content, re.DOTALL)
        if found:
            return _attach_page_guide(found.group(1).strip(), role)
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


# 节选截断（20260919）：旧版只留**末尾** per 字，长回复中段的指代锚点会被整段
# 截掉——实证（会话 144，1257 字回复）里点名的《架构文档》在正文中段，planner
# 眼里就成了"这轮从没提过这篇"，于是从零检索去找（还找错了另一篇）。改成头尾
# 各取一段，中段被截时先把其中的指代锚点（《标题》/id=/article 链接）捞回来附上。
_TAIL_MID_ANCHOR_RE = re.compile(r"《[^》\n]{2,40}》|id\s*[=:：]\s*\d+|/article/\d+")
_TAIL_HEAD = 80


def _clip_mid(text: str, head: int = _TAIL_HEAD, tail: int = 160) -> str:
    """头 80 + 中段锚点 + 尾 160（见上方注释；中段无锚点时退化为纯头尾取样）。"""
    if len(text) <= head + tail + 8:
        return text
    mid = text[head:len(text) - tail]
    uniq = list(dict.fromkeys(_TAIL_MID_ANCHOR_RE.findall(mid)))[:4]
    keep = ("（中段提到： " + " ".join(uniq) + " ）") if uniq else ""
    return text[:head] + " …" + keep + "… " + text[-tail:]


def _recent_tail(messages: list, max_turns: int = 4, per: int = 160) -> str:
    """最近几轮对话节选——**一问一答成对**渲染（planner 语境补丁，20260903 起）。

    planner 是单消息决策（history-blind），用户催促/质疑/短应答（"你不直接转跳
    过去？""要"）所指的对象只存在于更早轮次里——不给节选就无法还原该跳哪页。
    旧版把消息平铺成独立的"用户：…"/"泠月：…"行，谁接谁要靠数行位推断；邻接
    成对（20260920 批次 b）直接把"用户说了什么 → 泠月当时怎么回的"摆在眼前，
    并给最近一轮标记出它正是当前消息的应答对象。跳过两样：注入的页面上文
    （[System:…] 开头的人类消息，planner 已有 page_ctx）与当前这条用户消息
    （它是决策对象，不是上下文）。逐条 _clip_mid 截断防超长输入稀释决策
    （头尾取样 + 中段锚点打捞，20260919 起；纯尾部截断会丢文档名）。
    """
    turns: list[dict] = []
    for m in messages:
        if isinstance(m, (HumanMessage, AIMessage)):
            text = (_msg_text(m) or "").strip()
            if not text or (isinstance(m, HumanMessage) and text.startswith("[System:")):
                continue
            if isinstance(m, HumanMessage):
                turns.append({"u": text, "a": ""})
            elif turns and not turns[-1]["a"]:
                turns[-1]["a"] = text
            else:  # 没有前置用户消息的泠月发言（兜底补发轮）——单独占一轮
                turns.append({"u": "", "a": text})
    # 最后一条用户消息 = 当前请求（它后面没有泠月回复），不算上下文
    if turns and not turns[-1]["a"]:
        turns.pop()
    turns = turns[-max_turns:]
    if not turns:
        return "最近对话节选：（无更早轮次）"
    lines = []
    for i, t in enumerate(turns):
        ago = len(turns) - i  # 1 = 最近一轮
        user = _clip_mid(t["u"].replace("\n", " "), tail=per) if t["u"] else "（无）"
        ai = _clip_mid(t["a"].replace("\n", " "), tail=per) if t["a"] else "（未及回复）"
        mark = "　← 当前这条消息就是对这句的回应" if ago == 1 else ""
        lines.append(f"[上{ago}轮] 用户：{user}\n　　　　 泠月：{ai}{mark}")
    return ("最近对话节选（**一问一答成对**，最近一轮在最后——判断'催促/质疑/"
            "短应答'所指：目标通常就在紧邻的那条泠月发言里）：\n"
            + "\n".join(lines))


def _last_assistant_utterance(messages: list) -> str:
    """当前用户消息之前的**最近一条泠月发言**（短应答/催促/质疑的直接应答对象）。"""
    seen_current = False
    for m in reversed(messages):
        if isinstance(m, HumanMessage):
            text = (_msg_text(m) or "").strip()
            if text.startswith("[System:"):
                continue
            if not seen_current:  # 跳过当前请求本身，往前找
                seen_current = True
            continue
        if isinstance(m, AIMessage):
            text = (_msg_text(m) or "").strip()
            if text:
                return text
    return ""


# ── 短应答解析（20260920 批次 b）──
# 动机："要"/"好"/"不用了"/"算了"这类消息**本身不含任何意图**，含义完全由上一轮
# 泠月的提议决定（提议里往往就写着"要我把 X 读一遍吗"）。planner 是单消息决策，
# 平铺节选下这类消息极易被当成新话题（从零检索/答非所问）。这里把"上一轮泠月
# 到底提议了什么"确定性提取成一句直接指引，并把同意/拒绝两类分开给相反的动作
# 指令（同意 = 把提议那件事真的规划出来；拒绝 = 零调用收尾、绝不执行）。
# 只是一句提示（不改决策权）：判断仍归 planner，但它不再需要猜"要"指什么。
_SHORT_MAX = 12  # 去标点空白后的长度上限——超过就不是"短应答"
_PUNCT_ONLY_RE = re.compile(r"[\s，。！？~～、；：,.!?…·\-—_/\\|]+")
_SHORT_LEAD_RE = re.compile(r"^(那|那就|就|我|咱|我们|你|小猫咪|泠月|喵|，|,|、)+")
_SHORT_POS = frozenset({
    "要", "要的", "要啊", "好", "好的", "好啊", "好呀", "好吧", "嗯", "嗯嗯", "嗯好",
    "行", "行吧", "可以", "可以呀", "对", "对的", "对呀", "是", "是的",
    "查", "查吧", "查一下", "查查看", "看", "看一下", "看一眼", "看下", "读", "读吧",
    "读一下", "继续", "继续吧", "来", "来吧", "试", "试试", "搞", "搞吧",
    "需要", "麻烦你", "麻烦你了", "辛苦啦", "谢谢", "谢谢啦", "OK", "ok", "Ok", "okay",
})
_SHORT_NEG = frozenset({
    "不", "不用", "不用了", "不用啦", "不用了谢谢", "不要", "不必", "别", "别了",
    "算了", "算了不用", "不了", "先不", "先不用", "不查了", "不看了", "不用麻烦",
    "没事", "没事了", "取消", "免了", "回头再说", "下次吧", "晚点再说",
})
# 授权式短应答（20260923，P1）：主人**没有指定怎么做**，而是把选择权交给泠月
# （"小猫咪按你想法来吧""你看着办""都行"）。它与 pos 的区别是关键的：pos 承接的是
# "泠月提议的那件具体事"，auth 连**目标**都要泠月自己定——而"自己定"只能从系统
# 数据里定（待办候选/上轮提议），绝不能从模型历史里挑一条自然语言当目标。
# 事故源头正是后者：20260923 13:19 那条 trace 里，narrator 回了一个**来自历史**的
# 旧留言（且报了待确认），而系统里真正待审的是另一条。
_SHORT_AUTH = frozenset({
    "按你想法来吧", "按你想法来", "按你的想法来", "按你想的来", "按你的来吧",
    "按你意思来", "按你的意思办", "听你的", "听你的吧", "都听你的", "全听你的",
    "随你", "随你吧", "随你便", "随你安排", "都随你", "依你", "依你吧",
    "看着办", "你看着办", "看着来吧", "拿主意", "你拿主意", "说了算", "你说了算",
    "随便", "随便你", "你决定", "你定", "你定吧", "决定", "安排", "你安排",
    "都行", "怎么都行", "都成", "都可以", "怎么都可以", "都听你的安排",
})
# 上表的形态千变万化（"按你的想法来办吧"这种加字），一个整串集合追不全 ⇒ 补一条
# **锚定式**正则（^…$ 整串，故不会在长句里误命中）。三族：①"按/照/依/听/随 + 你"；
# ②"看着办/决定/说了算/拿主意/安排"（`_SHORT_LEAD_RE` 已把句首的"你"剥掉，
# 所以"你看着办"到这里就是"看着办"）；③"都/全 + 行/可以/好/成"（**必须带"都/全"**
# ——裸"好/行/可以"在 `_SHORT_POS` 里，属同意而非授权）。
_SHORT_AUTH_RE = re.compile(
    r"^(?:都|全|一切|就|那)?(?:"
    r"(?:按|照|依|听|随)(?:你|您|主人)(?:的|着)?"
    r"(?:想法|意思|主意|安排|便|来|办|选|定)?(?:(?:来|办|吧|了|就行|就好)){0,3}"
    r"|看着(?:办|来)(?:吧|了)?"
    r"|(?:决定|说了算|拿主意|安排|定夺)(?:吧|了)?"
    r"|(?:怎么|怎样)?(?:都|全)(?:行|可以|好|成)(?:吧|了)?"
    r")$")


def _short_reply_kind(text: str) -> str:
    """短应答分类：'pos'（同意/要求继续）/ 'neg'（拒绝/收回）/ 'auth'（授权泠月
    自己定目标）/ ''（不是短应答）。

    ⚠️ **入口先剥系统消息壳（20260923 修）**：`server.py` 给本轮用户消息加锚点壳
    `[当前问题]: `，而本判据是**整串相等**型（剥标点/称呼后与 `_SHORT_POS`/`_SHORT_NEG`
    对表）——壳里的 `[` `]` `:` 既不在 `_PUNCT_ONLY_RE` 的剔字表里、也不在 `_SHORT_LEAD_RE`
    的称呼表里，于是 core 恒为 `[当前问题]: 好` 这种形态 ⇒ 永远返回 ''。
    实证：golden 用例 followup_short_yes_executes（用户只回一个「要」）的 trace 里
    planner 收到的 `short_reply` 字段写的是「（当前消息不是短应答）」，即该确定性提示
    自 20260920 批次 b 上线起在各处恒不触发（那两条用例仍 PASS = LLM 自己读懂了，
    属"少一层助力"而非观测到的故障）。同款坑第二例，第一例见 decisions.py 的导航快道。
    **只影响判定，不碰给模型的 prompt**（壳是给模型看的锚点，不是缺陷）。
    """
    core = _SHORT_LEAD_RE.sub("", _PUNCT_ONLY_RE.sub("", strip_system_tags(text or ""))).strip()
    if not core or len(core) > _SHORT_MAX:
        return ""
    if core in _SHORT_NEG:  # 先否定：三集合无交集，顺序只为可读
        return "neg"
    if core in _SHORT_AUTH or _SHORT_AUTH_RE.match(core):
        return "auth"
    if core in _SHORT_POS:
        return "pos"
    return ""


def _short_reply_hint(messages: list, system_facts: str = "") -> str:
    """短应答提示块（planner 模板 {short_reply_hint}）；非短应答给缺省语。

    `system_facts`（20260923 P2）：随本轮从**系统台账**读来的候选清单，只在授权式
    那一类后面追加——授权式的目标不许从模型历史里挑，只能从这份台账里定，而
    "台账里到底有哪几条"必须由系统给（模型自己编不出、也不许编）。给空串则与旧
    行为逐字相同（非授权式轮次调用点根本不传）。
    """
    user_msg = _last_user_msg(messages)
    kind = _short_reply_kind(user_msg)
    if not kind:
        return "（当前消息不是短应答）"
    last = _last_assistant_utterance(messages)
    if not last:
        return (f"当前消息「{user_msg}」是短应答，但本会话此前没有泠月的发言可承接"
                "——按字面做最保守的解读，不要凭空补出一个动作。")
    ai = _clip_mid(last.replace("\n", " "), tail=200)
    if kind == "pos":
        act = ("判定：这是对上一轮提议的**同意/要求继续** → 把泠月提议的那件事真的规划"
               "出来执行（该点名的工具照常点名、参数填全），不得只口头答应，也不得另开"
               "新话题或换一件事做。")
    elif kind == "auth":
        act = ("判定：这是**授权式**应答——主人把「做哪一件」的决定权也交给了泠月。"
               "此时**目标必须从系统数据里定**（本轮的待办/待审清单、上一轮泠月发言里"
               "点过名的那件事、工具回执），**不许从历史对话里挑一条自然语言当目标**"
               "（那是编造）；候选唯一 → 把那一件真的规划出来执行（写操作该走确认的"
               "照常走）；候选不唯一或查不到 → 本轮零写、如实说明有哪几个候选并请主人"
               "点名，**绝不替主人选**，也绝不声称已经发起或已经确认。")
    else:
        act = ("判定：这是对上一轮提议的**拒绝/收回** → 本轮不规划任何工具，零调用收尾，"
               "简短确认「好，那就不做了」；不得再执行那个动作，也不得声称已经做了什么。")
    text = (f"当前消息是**短应答**（「{user_msg}」）——它本身不含意图，含义由上一轮泠月"
            f"的发言决定：\n　　泠月：{ai}\n{act}")
    if kind == "auth" and system_facts:
        text += "\n" + system_facts
    return text


def _has_frames(messages: list) -> bool:
    """当前请求是否有工具执行帧（ToolMessage）。"""
    return any(isinstance(m, ToolMessage) for m in messages)


# ── 本会话已点名文档（20260919）──
# 动机（实证事故，会话 144 / 20260919 17:18:45）：planner 是单消息决策且历史只以
# _recent_tail（最近 4 条 × 头尾取样）出现；规则 4 的文章指代解析又要求消息里带
# 指代词（"那篇/这篇/它"）——用户只问"你看了吗就说没写"（对上文的追问，无指代词）
# 时整条规则不启动，落规则 3"机制型 → rag_search 发用户原句"。而事实上 page_ctx 的
# recent_executions 里当时就有「读取文章 19《Saudade Blog AI Agent（泠月喵）架构
# 文档》」两行、上一轮回复也点了名：文档是**明确的**，planner 却从零检索，BM25 命中
# 了同主题的另一篇（46《文章向量空间图谱项目文档》），确定性拦截再把它读全文 →
# 整轮跑偏（用户原话"明明上下文都明确文档是什么，结果还要走一遍 rag 去找文章"）。
# 解法与 intent_hints 同构：系统把锚点确定性抽出来注入（给事实、不夺决策——用户
# 到底指哪篇仍由 planner 判断），planner 不必为了"知道是哪篇"再跑检索。
_DOC_TITLE_RE = re.compile(r"《([^》\n]{2,60})》")
# 跨轮执行记忆的动作行（Rust render_exec_row 的产物，"读取文章 19《标题》"）
_DOC_READ_ROW_RE = re.compile(r"读取文章\s*(\d+)\s*《([^》\n]{1,60})》")
# 文章链接 = **唯一被认的邻域 id 形态**。裸 `id=N` 已于 20260925 摘除：它不具名，
# 邻域里任何一个 id 都能被认成文章 id——线上实测把**标签 id** 认成了文章 id
# （会话摘要写「为文章《Python asyncio 异步并发》添加…同**名一级标签（id=19）**」
# ⇒ 锚点产出《Python asyncio 异步并发》 id=19，而 19 其实是另一篇文章）。
_ARTICLE_PATH_RE = re.compile(r"/article/(\d+)")
_DOC_ID_WINDOW = 48   # 《标题》前后多少字符内出现的 /article/ 链接算这篇的 id
# markdown 链接当标题的写法（《[标题](url)》）：标题是标签文字，url 只是 id 来源
_MD_LINK_RE = re.compile(r"\[([^\]]*)\]\(([^)]*)\)")
# 窗口内的 id 还必须**与标题同一个句读单位**（20260925 二次收紧）：48 字窗口只挡了
# "跨过下一条标题"，没挡"跨过句号"。线上实测（trace 20260920T140051 的回复正文）
# 《IoT 设备接入物联网平台指南》后面那句「想直接看这条说说的原文，可以点
# [ESP32-S3-OBC固件接入参考](…/article/46)」把 46 记到了 IoT 头上——46 是另一篇。
# 中文、字母数字、句读、方括号任一出现即判为"另一个句子/另一条链接"。
_DOC_LINK_GAP_RE = re.compile(r"[\w\[\]。！？；，、\n\r]")

# 简称判定阈值（见 _doc_anchors 内注释：宁可漏并、绝不错并——错并会把 A 篇的 id
# 挂到 B 篇名下，正是本次要修的故障形态）
_DOC_BRIEF_MIN = 7      # 共享连续子串最短字符数
_DOC_BRIEF_RATIO = 0.7  # 且须占较短标题的这么多个比例


def _lcs_contig(a: str, b: str) -> int:
    """最长**连续**公共子串长度（标题都很短，朴素 DP 足够）。"""
    if not a or not b:
        return 0
    prev = [0] * (len(b) + 1)
    best = 0
    for ca in a:
        cur = [0] * (len(b) + 1)
        for j, cb in enumerate(b, 1):
            if ca == cb:
                cur[j] = prev[j - 1] + 1
                if cur[j] > best:
                    best = cur[j]
        prev = cur
    return best


def _brief_same(a: str, b: str) -> bool:
    """两条标题是否同篇的简/全称（同一篇被正文简称与执行记忆全称各提一次）。"""
    short, long_ = (a, b) if len(a) <= len(b) else (b, a)
    if len(short) < _DOC_BRIEF_MIN:
        return False
    return _lcs_contig(short, long_) >= max(_DOC_BRIEF_MIN,
                                            int(len(short) * _DOC_BRIEF_RATIO))


def _doc_id_lookup(title: str) -> str:
    """标题 → 站内文章 id（确定性解析；不确定/索引未就绪 ⇒ ""）。

    20260920 方案①：只有标题、历史里从没见过 id 的锚点，planner 只能去
    list_notes 里猜下标——线上实测把分页列表第一条（最新那篇）当成"用户点名的
    这篇"，读了错文章、还谎称站内没有该文（真文 note 14 存在）。语料索引里本来
    就有全部可见文章的标题与 id（rag/search.py，与前台可见性一致），直接解析：
    **唯一命中才给 id**。解析不出来（歧义、草稿、索引未就绪、根本不是文章）一律
    返回 ""——调用方据此**丢弃**这条（20260925 前是留着写"（未见过 id）"，见
    `_doc_anchors` 头注）。
    """
    try:
        from rag.search import resolve_title
        rid = resolve_title(title)
    except Exception:            # 解析降级绝不能拖垮上下文组装
        return ""
    return str(rid) if rid is not None else ""


def _corpus_ready() -> bool:
    """语料索引快照是否可用（只看"有没有内容"，不建索引、不阻塞）。"""
    try:
        from rag.search import get_index
        return bool(get_index().docs_snapshot())
    except Exception:
        return False


def _bounded_after(s: str) -> str:
    """邻域截到**下一条标题之前**（《 或 》 都算边界）。"""
    cut = len(s)
    for ch in "《》":
        i = s.find(ch)
        if i >= 0:
            cut = min(cut, i)
    return s[:cut]


def _bounded_before(s: str) -> str:
    """邻域只留**上一条标题之后**的那一段（前面的标题与其邻域不许越界）。"""
    cut = 0
    for ch in "《》":
        i = s.rfind(ch)
        if i >= 0:
            cut = max(cut, i + 1)
    return s[cut:]


def _clean_title(t: str) -> str:
    """标题净化：`《[标题](url)》` 里标题只是标签文字（url 是 id 来源）；
    `《**标题**》` 这种加粗写法把星号留在书名号里（实测锚点清单里出现过
    `《**IoT 设备接入物联网平台指南**》`）。"""
    return _MD_LINK_RE.sub(r"\1", t).strip().strip("*`_ \t")


def _linked_doc_id(text: str, tm) -> str:
    """《标题》对应的文章 id——**只有结构性绑定才算**，取不到返回 ""（不猜）。

    三个位置，其余一律不认：
    ① `/article/N` 就在《》**之内**：`《[标题](/article/22)》`；
    ② 标题与一条 markdown 链接**同一句读单位内相邻**，且链接文字与标题指同一篇
       （`[《TEST8》](…)` 标题即链接文字；`《甲文档》([甲](…))` 两边文字互相包含）；
       相邻 = 之间只有标点/空白（`**(`、`（链接 `），没有中文、字母数字、句读、方括号。
    ③ 同上相邻，但链接是**裸路径**（`《X》**（/article/9）`）。
    窗口内第一条链接不合规就**放弃**（不往后找第二条）——往后找正是旧窗口的病根；
    也正因如此，`《IoT…指南》…可以点 [ESP32-S3-OBC固件接入参考](…/article/46)`
    这种「标题后面那句里另有一条链接」不会再被记到标题头上。
    """
    inner = _ARTICLE_PATH_RE.search(tm.group(0))
    if inner:
        return inner.group(1)
    tkey = _clean_title(tm.group(1)).replace(" ", "").lower()
    for lm in _MD_LINK_RE.finditer(text):
        lt = _clean_title(lm.group(1)).replace(" ", "").lower()
        if not lt:
            continue
        if lm.start(1) <= tm.start() and tm.end() <= lm.end(1):     # 标题就是链接文字
            m = _ARTICLE_PATH_RE.search(lm.group(2))
            return m.group(1) if m else ""
        if not (lt in tkey or tkey in lt):                         # 同名才算同篇
            continue
        if lm.end(1) <= tm.start():
            gap = text[lm.end(1):tm.start()]
        elif lm.start() >= tm.end():
            gap = text[tm.end():lm.start()]
        else:
            continue
        if not _DOC_LINK_GAP_RE.search(gap):
            m = _ARTICLE_PATH_RE.search(lm.group(2))
            return m.group(1) if m else ""
    tail = _bounded_after(text[tm.end():tm.end() + _DOC_ID_WINDOW])
    m = _ARTICLE_PATH_RE.search(tail)
    if m and not _DOC_LINK_GAP_RE.search(tail[:m.start()]):
        return m.group(1)
    head = _bounded_before(text[max(0, tm.start() - _DOC_ID_WINDOW):tm.start()])
    hits = list(_ARTICLE_PATH_RE.finditer(head))     # 反向往前取**最近**那条
    if hits:
        m = hits[-1]
        if not _DOC_LINK_GAP_RE.search(head[m.end():]):
            return m.group(1)
    return ""


def _doc_anchors(messages: list, limit: int = 6, budget: int = 700) -> str:
    """本会话已点名文档清单（《标题》+ id + 是否已读全文）——跨轮指代的确定性锚点。

    来源 = 全部窗口消息（[System:…] 的 recent_executions 行 + 20 条人机历史，
    含工具帧之外的正文），**由近及远**扫描：最近被点到的排前面（越近越可能是
    用户在说的那篇）。同一篇被正文简称与执行记忆全称各提一次时并成一行（简称
    留作别名），免得 planner 把一篇数成两篇。

    **进清单的门（20260925 收紧）：一条《…》要么带**有来源的 id**，要么能解析成
    站内文章；两者都不满足就**丢掉**。** 起因是线上实测「假文章污染」：

    - 旧规则把「同一句里邻域的 `id=N`」也当文章 id，可 id 不具名 ⇒ 会话摘要里
      「为文章《Python asyncio 异步并发》添加…同名一级标签（id=19）」让锚点产出
      `《Python asyncio 异步并发》 id=19`（19 是另一篇文章），而规则 4⓪ 授权
      planner **直接采用清单里的 id** ⇒ 会去读错的那一篇。现在邻域只认
      `/article/N`（URL 路径本身就是文章命名空间）。
    - 48 字窗口也不够：它只挡了"跨过下一条标题"，没挡"跨过句号"。实测
      `《IoT 设备接入物联网平台指南》…可以点 [ESP32-S3-OBC固件接入参考](…/article/46)`
      把 46 记到了 IoT 头上（46 是另一篇）。现在要求链接与标题**同一句读单位**
      且**链接文字与标题指同一篇**（见 `_linked_doc_id`）。
    - 旧规则对解析不出的标题仍写「（未见过 id）」并留在清单里，可抬头声称的是
      "**已点名文档**"。实测这一栏里混着站内通知《留言未通过审核》、公告
      《中秋快乐》《今晚不许熬夜！》《致李重九》——它们在对话里本来就写作
      `26《标题》`（实体摘要格式），与文章读取行同形 ⇒ 被当成了文档。
    - 语料索引未就绪时（解析全返回 ""）不再逐条降级成假象，改为在块尾如实注记。

    **id 只有两种来源**：跨轮执行记忆的 `读取文章 N《标题》` 行（系统写的、文章
    命名空间，**最高可信**），与结构性绑定的 `/article/N` 链接（自由文本里的，
    可能本身是叙述幻觉 ⇒ 语料能唯一定位时以语料为准，链接 id 只用于草稿这类
    语料里没有的篇目）。两者都没有、语料也解析不出的标题会被丢掉——当前消息
    本身永远全文可见，真要找它还有规则 4③ 的 list_notes 通道。
    """
    rows: list[tuple[str, str, bool, str]] = []   # (标题, id 或 "", 是否已读全文, 简称)

    def _add(title: str, doc_id: str, read: bool) -> None:
        title = title.strip()
        if not title:
            return
        key = title.replace(" ", "").lower()
        for i, (t, d, r, alias) in enumerate(rows):  # 同篇合并：已读优先、id 补齐、长标题优先
            tn = t.replace(" ", "").lower()
            same = tn == key or bool(doc_id and d == doc_id)
            # 简称 ↔ 全称同篇（实测：正文口语简称《AI Agent 架构文档》，而跨轮执行记忆里
            # 是全称《Saudade Blog AI Agent（泠月喵）架构文档》——两者不是简单包含关系，
            # 中间夹着"（泠月喵）"，且**各自出现的那条消息里都还没有 id**，id 在更早的
            # 执行记忆行上）。判据 = 共享连续子串够长（≥_DOC_BRIEF_MIN 且占短标题
            # ≥_DOC_BRIEF_RATIO）；短于 _DOC_BRIEF_MIN 的标题一律不参与，防"物联网平台"
            # 并进"物联网平台接入指南"这类**不同**文章；两条都带 id 且不同则绝不并
            # （错并会把 A 篇 id 挂到 B 篇名下 —— 正是本次要修的故障形态，宁可漏并不错并）。
            brief = (not same and not (doc_id and d and doc_id != d)
                     and _brief_same(key, tn))
            if not (same or brief):
                continue
            long_t = title if len(title) > len(t) else t
            short_t = t if long_t == title else title
            alias = (alias if not brief else
                     min([x for x in (alias, short_t) if x and x != long_t],
                         key=len, default=""))
            rows[i] = (long_t, d or doc_id, r or read, alias)
            return
        rows.append((title, doc_id, read, ""))

    for m in reversed(messages):                 # 由近及远
        text = _msg_text(m) or ""
        if not text:
            continue
        for dm in _DOC_READ_ROW_RE.finditer(text):        # 跨轮执行记忆的读取行
            _add(dm.group(2), dm.group(1), True)
        for tm in _DOC_TITLE_RE.finditer(text):
            # 邻域只认 `/article/N`（20260925：裸 `id=N` 不具名，会把标签/通知/留言
            # 的 id 当文章 id）。标题**之内**也要看（《[标题](/article/13)》），
            # 邻域**不许跨过另一条标题**（否则 A 篇后面那句里的链接会记到 A 头上）。
            # 邻域 id 只认**结构性绑定**（见 _linked_doc_id）：裸 `id=N` 不具名，
            # 48 字窗口内的链接也不一定属于本篇（跨句就会记错）。
            _add(_clean_title(tm.group(1)), _linked_doc_id(text, tm), False)

    if not rows:
        return "（本会话还没有点名的文档）"
    lines: list[str] = []
    used = 0
    dropped = 0
    seen_ids: set[str] = set()
    for title, doc_id, read, alias in rows[:limit]:
        resolved = False
        if doc_id and not read:
            # id 来自**自由文本里的链接**——链接本身可能是叙述幻觉（实测
            # `[ESP32-S3-OBC固件接入参考](…/article/46)` 就指着一篇不相干的文章）
            # ⇒ 语料能唯一定位就用语料那个；语料里查不到（草稿）才退用链接里的。
            want = _doc_id_lookup(title)
            if want:
                doc_id, resolved = want, True
        if not doc_id:
            doc_id = _doc_id_lookup(title)
            resolved = bool(doc_id)
            if not doc_id:
                dropped += 1     # 没有来源也不像站内文章 ⇒ 不进清单（见函数头注）
                continue
        if doc_id and doc_id in seen_ids:
            continue             # 两种写法解析到同一篇 ⇒ 只留最近那条（同 id 必同篇）
        if doc_id:
            seen_ids.add(doc_id)
        line = f"· 《{title}》 id={doc_id}"
        marks = (["本会话已读过全文"] if read else [])
        if resolved:
            marks.append("站内标题匹配")
        if alias:
            marks.append(f"上文亦称《{alias}》")
        if marks:
            line += "（" + "；".join(marks) + "）"
        if used + len(line) > budget:
            break
        used += len(line)
        lines.append(line)
    if dropped and not _corpus_ready():
        # 索引未就绪时解析一律返回 "" ⇒ 上面那些**不是**"站内没有这篇"，而是
        # "这会儿查不了"。别让 planner 从一条空清单里读出"本会话没提过文章"。
        lines.append("（语料索引未就绪：以上只含带 id 的条目，标题解析暂不可用）")
    if not lines:
        return "（本会话还没有点名的文档）"
    return "\n".join(lines)


# get_article_detail 全文帧的节选上限：该帧是 planner 决策与 narrator 引用文章细节的
# 依据，深文事实常在文末（20260903 golden 实证：架构文档 note 19 的
# STREAM_TOTAL_TIMEOUT 在全文 19260 字符处、固件参考 note 14 的 esp_https_ota
# 在 3754 处，per=300 的旧截断让整条 rag 深文族 FAIL）。
#
# 20260925 从 20000 提到 28000，依据是**实测**而不是"覆盖全部文章"那句话（旧注释这么写，
# 而它早已不成立）：`sections.slim_frame` 在造帧时去掉同文本的 `content` 重复键后，
# 站内 11 篇里最长的帧 = 26,794 字（note 19；次长 note 46 = 20,552），28000 留 ~4.5% 余量。
# 它是**单帧预算**，超了走 `sections.frame_excerpt` 的按节节选（整节取舍 + 文末列未展开
# 小节 + 取回方式，不是无声截断）——这条保底路径必须一直有效。
#
# 谁来看住这个数：`eval/frame_budget.py`（哨兵，夜间跑）拿同一对函数算出每篇的帧长，
# 报出"有哪几篇超预算、超多少"，非门禁。**不要**把它改成"按语料最长文章自动派生"：
# planner 的提示词规模一旦跟着站点内容浮动，发一篇长文就会静默改变决策侧的上下文
# （而且每次跑还不一样）；这里要的是"预算固定 + 超了看得见"。
_DETAIL_FRAME_PER = 28000


# ── 列表帧的紧凑渲染（20260921）────────────────────────────────────────
# 背景：普通帧此前是 `text[:300]` **裸切**——列表帧（JSON/`repr` 数组）会在**一行
# 中间**断掉，planner 拿到的是半截 JSON，既读不出后半条的 id，也**看不出后面还有
# 多少条**（与"空结果 vs 没执行"同源的无声失真）。现在分两步：先压成"一行一条"
# （只留标量字段、长值截断、封面/焦点缩放这类纯展示字段丢掉），再**按整行**取舍，
# 并在文末如实写明「节选：显示前 K 条，共 N 条」。
#
# 为什么敢丢字段：narrator 手上还有**完整的 ToolMessage**（model 节点把
# state["messages"] 一起喂给 LLM），这里的文本只进 planner 的提示词与 narrator 的
# 系统摘要——是"给决策者的缩略视图"，不是唯一证据。
_FRAME_NOISE_KEYS = {"cover", "coverFocusX", "coverFocusY", "coverZoom",
                     "carouselFocusX", "carouselFocusY", "carouselZoom"}
_FRAME_FIELD_CAP = 60      # 单字段值的字符上限（超出带 … 标记）


def _compact_row(row: dict) -> str:
    """一行记录 → `k=v k=v`（丢空值/纯展示字段/嵌套结构，长值截断带 …）。"""
    parts = []
    for k, v in row.items():
        if k in _FRAME_NOISE_KEYS or v is None or v == "" or isinstance(v, (dict, list)):
            continue
        s = str(v)
        if len(s) > _FRAME_FIELD_CAP:
            s = s[:_FRAME_FIELD_CAP] + "…"
        parts.append(f"{k}={s}")
    return " ".join(parts)


def _compact_head(obj: dict) -> str:
    """信封 dict 里**与数组同级**的标量 → 一行抬头（`k=v k=v`）。

    20260924 补：此前信封只取数组、同级字段**整个丢掉**——`{unread: 2, items: […]}` 的
    帧里永远看不到 `unread=2`，`get_unread_summary` 那类"计数 + 明细"的返回一旦带上
    明细，计数就没了（用户问"我有几条未读"反而取不到那个数）。丢的是**决策依据**，
    不是展示噪音，所以补一行抬头；行的取舍预算照旧只算明细行。
    """
    parts = []
    for k, v in obj.items():
        if k in _FRAME_NOISE_KEYS or v is None or v == "" or isinstance(v, (dict, list)):
            continue
        if not isinstance(v, (str, int, float, bool)):
            continue
        s = str(v)
        if len(s) > _FRAME_FIELD_CAP:
            s = s[:_FRAME_FIELD_CAP] + "…"
        parts.append(f"{k}={s}")
    return " ".join(parts)


def _compact_list_frame(text: str, budget: int) -> str | None:
    """数组帧 → "一行一条"紧凑文本（超预算按**整行**取舍并标注共几条）。

    认不出（不是数组/元素不是 dict）返回 None，调用方按普通文本处理。
    两种字面量都要认：`str(data)` 出来的是 **Python repr**（单引号，tools 层
    绝大多数工具的出口），少数工具是 `json.dumps`（双引号）。
    信封形态（`{"unread": 2, "items": […]}`）先出一行**同级标量抬头**（见 `_compact_head`）。
    """
    obj = None
    for loader in (ast.literal_eval, json.loads):
        try:
            obj = loader(text)
            break
        except Exception:      # noqa: BLE001 —— 不是字面量就走普通文本分支
            continue
    rows = None
    head = ""
    if isinstance(obj, list):
        rows = obj
    elif isinstance(obj, dict):
        for k in ("data", "records", "list", "items"):
            if isinstance(obj.get(k), list):
                rows = obj[k]
                break
        if rows is not None:
            head = _compact_head(obj)
    if not rows or not all(isinstance(r, dict) for r in rows):
        return None
    lines: list[str] = []
    used = len(head) + 1 if head else 0
    for i, r in enumerate(rows, 1):
        body = _compact_row(r)
        if not body:
            continue
        line = f"{i}. {body}"
        if used + len(line) + 1 > budget:
            if lines:
                out = ("\n".join(lines)
                       + f"\n（节选：显示前 {len(lines)} 条，共 {len(rows)} 条）")
                return f"{head}\n{out}" if head else out
            # 第一条就超预算：单条过长，截断并说明（不静默）
            cut = f"1. {body[:budget]}…（单条过长已截断，共 {len(rows)} 条）"
            return f"{head}\n{cut}" if head else cut
        lines.append(line)
        used += len(line) + 1
    if not lines:
        return None
    out = "\n".join(lines)
    return f"{head}\n{out}" if head else out


def _frame_texts(messages: list, limit: int = 5, per: int = 300) -> str:
    """最近的工具返回摘要（planner 下一轮决策依据 / narrator 叙述依据）。

    只取最近 limit 条。截断策略按帧型：普通帧（检索候选/列表）截 per 字符
    （行式精简，够看）；get_article_detail 是全文读取帧，按 _DETAIL_FRAME_PER
    大幅放宽并标注"节选"；__ERROR__ 信息完整保留（planner 需要据错误修正参数
    重试）；**列表帧走 _compact_list_frame**（一行一条 + 整行取舍 + 共几条），
    不再裸切半行。
    """
    frames = [m for m in messages if isinstance(m, ToolMessage)]
    if not frames:
        return "（本轮尚无工具执行）"
    parts = []
    for m in frames[-limit:]:
        name = getattr(m, "name", "") or ""
        text = _msg_text(m)
        if text.strip() in ("", "[]", "{}"):
            # 20260920：空结果必须与"没执行"在措辞上分开——真实事故里 narrator 把
            # `返回: []` 读成了"本轮没有执行任何工具（回执为空）"（graph.py 的
            # _NO_EXEC_CLAIM_RE 是兜底，这里是治本：把"执行了，只是空"写明白）。
            parts.append(f"工具 {name} 返回（**已执行，结果为空**）: "
                         f"{text.strip() or '[]'}")
        elif text.startswith("__ERROR__"):
            parts.append(f"工具 {name} 返回错误: {text}")
        elif name == "get_article_detail" and len(text) <= _DETAIL_FRAME_PER:
            parts.append(f"工具 {name} 返回: {text}")
        elif name == "get_article_detail":
            # 20260920：超长文章改**按小节**取舍（sections.frame_excerpt），不再逐字硬截。
            # 旧实现（`text[:20000]`）的问题是**无声**——正文在一句话中间断掉，模型
            # 连"后面还有内容、缺的是哪几节"都不知道（实测 note 19 = 25,445 字，
            # §7-§10 从未进过任何一轮上下文）。现在超限时整节取舍 + 文末列未展开
            # 小节名 + 给出取回方式（get_article_detail(section=…)）。
            # 未超限的帧原样透出（上面那支）：既有行为不动，边界只影响超长文章。
            cut = sections.frame_excerpt(text, _DETAIL_FRAME_PER)
            if sections.UNEXPANDED_MARK in cut:
                parts.append(
                    f"工具 {name} 返回（原文 {len(text)} 字，超单帧上限，已按小节节选；"
                    f"未展开的小节见文末清单，可按需再读）: {cut}")
            else:
                parts.append(
                    f"工具 {name} 返回（节选，原文 {len(text)} 字，仅示前 {len(cut)} 字）: "
                    f"{cut}")
        else:
            compact = _compact_list_frame(text, per)
            if compact is not None:
                parts.append(f"工具 {name} 返回: {compact}")
            elif len(text) > per:
                # 非列表帧也可能超预算（长文本/大对象）：同样如实标注，不裸切
                parts.append(f"工具 {name} 返回（节选，原文 {len(text)} 字）: "
                             f"{text[:per]}…")
            else:
                parts.append(f"工具 {name} 返回: {text}")
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
        res = str(r.get("result", ""))[:160]
        if res.strip() in ("", "[]", "{}"):
            # 同 _frame_texts：空结果标注"已执行"，否则 `→ []` 会被读成"没执行"
            res = "（已执行，结果为空）"
        lines.append(f"- {r['tool']} args={args_txt} → {res}")
    return "\n".join(lines)
