"""上下文组装（20260912 从 graph.py 拆出）——纯函数、零 LLM、零 LangGraph 依赖。

内容 = 注入给 planner/model 的事实文本生成：消息文本提取（多模态兼容）、页面
上下文 page_ctx、页面操作指南（GUESTBOOK_GUIDE / SITE_GUIDE）、工具帧摘要
（_frame_texts）、checker 回执摘要（_receipts_text）。

拆出动机：graph.py 曾是 2000+ 行单体——图拓扑（节点/边/路由）与该层纯文本
组装混在一起，读一处要翻半屏无关代码。本层被 graph 的节点调用，自身不依赖
图（decisions.py 也用它），是天然的叶子层。
"""

import json
import re

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

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


def _short_reply_kind(text: str) -> str:
    """短应答分类：'pos'（同意/要求继续）/ 'neg'（拒绝/收回）/ ''（不是短应答）。"""
    core = _SHORT_LEAD_RE.sub("", _PUNCT_ONLY_RE.sub("", text or "")).strip()
    if not core or len(core) > _SHORT_MAX:
        return ""
    if core in _SHORT_NEG:  # 先否定：两集合无交集，顺序只为可读
        return "neg"
    if core in _SHORT_POS:
        return "pos"
    return ""


def _short_reply_hint(messages: list) -> str:
    """短应答提示块（planner 模板 {short_reply_hint}）；非短应答给缺省语。"""
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
    else:
        act = ("判定：这是对上一轮提议的**拒绝/收回** → 本轮不规划任何工具，零调用收尾，"
               "简短确认「好，那就不做了」；不得再执行那个动作，也不得声称已经做了什么。")
    return (f"当前消息是**短应答**（「{user_msg}」）——它本身不含意图，含义由上一轮泠月"
            f"的发言决定：\n　　泠月：{ai}\n{act}")


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
_DOC_ID_NEAR_RE = re.compile(r"id\s*[=:：]\s*(\d+)")
# 跨轮执行记忆的动作行（Rust render_exec_row 的产物，"读取文章 19《标题》"）
_DOC_READ_ROW_RE = re.compile(r"读取文章\s*(\d+)\s*《([^》\n]{1,60})》")
_ARTICLE_PATH_RE = re.compile(r"/article/(\d+)")
_DOC_ID_WINDOW = 48   # 《标题》前后多少字符内出现的 id=/article/ 链接算这篇的 id

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
    **唯一命中才给 id**，有歧义就留"（未见过 id）"让 planner 按规则 4③ 去查。
    """
    try:
        from rag.search import resolve_title
        rid = resolve_title(title)
    except Exception:            # 解析降级绝不能拖垮上下文组装
        return ""
    return str(rid) if rid is not None else ""


def _doc_anchors(messages: list, limit: int = 6, budget: int = 700) -> str:
    """本会话已点名文档清单（《标题》+ id + 是否已读全文）——跨轮指代的确定性锚点。

    来源 = 全部窗口消息（[System:…] 的 recent_executions 行 + 20 条人机历史，
    含工具帧之外的正文），**由近及远**扫描：最近被点到的排前面（越近越可能是
    用户在说的那篇）。id 只认同一句里 《标题》 邻域的 id=/article 链接或
    "读取文章 N《标题》" 行；都没有时再按标题查站内语料（_doc_id_lookup，唯一
    命中才认）——不猜、不给没有依据的 id。同一篇被正文简称与执行记忆全称各提
    一次时并成一行（简称留作别名），免得 planner 把一篇数成两篇。
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
            # id 只在**标题之后**的邻域里认（"《X》**（id=19）"、"[《X》](/article/13)"；
            # 先查后邻域再查前邻域，且前邻域仅作兜底——否则会认成上一条目的 id）
            after = text[tm.end():tm.end() + _DOC_ID_WINDOW]
            nid = (_DOC_ID_NEAR_RE.search(after) or _ARTICLE_PATH_RE.search(after))
            if not nid:
                before = text[max(0, tm.start() - _DOC_ID_WINDOW):tm.start()]
                nid = (_DOC_ID_NEAR_RE.search(before) or _ARTICLE_PATH_RE.search(before))
            _add(tm.group(1), nid.group(1) if nid else "", False)

    if not rows:
        return "（本会话还没有点名的文档）"
    lines: list[str] = []
    used = 0
    seen_ids: set[str] = set()
    for title, doc_id, read, alias in rows[:limit]:
        resolved = False
        if not doc_id:
            doc_id = _doc_id_lookup(title)
            resolved = bool(doc_id)
        if doc_id and doc_id in seen_ids:
            continue             # 两种写法解析到同一篇 ⇒ 只留最近那条（同 id 必同篇）
        if doc_id:
            seen_ids.add(doc_id)
        line = f"· 《{title}》" + (f" id={doc_id}" if doc_id else "（未见过 id）")
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
    return "\n".join(lines)


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
        if text.strip() in ("", "[]", "{}"):
            # 20260920：空结果必须与"没执行"在措辞上分开——真实事故里 narrator 把
            # `返回: []` 读成了"本轮没有执行任何工具（回执为空）"（graph.py 的
            # _NO_EXEC_CLAIM_RE 是兜底，这里是治本：把"执行了，只是空"写明白）。
            parts.append(f"工具 {name} 返回（**已执行，结果为空**）: "
                         f"{text.strip() or '[]'}")
        elif text.startswith("__ERROR__"):
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
        res = str(r.get("result", ""))[:160]
        if res.strip() in ("", "[]", "{}"):
            # 同 _frame_texts：空结果标注"已执行"，否则 `→ []` 会被读成"没执行"
            res = "（已执行，结果为空）"
        lines.append(f"- {r['tool']} args={args_txt} → {res}")
    return "\n".join(lines)
