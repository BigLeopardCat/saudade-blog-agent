# -*- coding: utf-8 -*-
"""文章分节（20260920）：把 markdown 正文按标题切成小节——**一个实现，三处共用**。

**为什么住在 `rag/` 而不是 `agent/`**（20261005 从 `agent/sections.py` 搬来）：消费者
有三处，其中两处在 agent 层**下面**（`rag/search.py` 索引切片、`tools/base.py` 按节
取回），第三处才是渲染侧 `agent/context.py`。原先它住在 `agent` 包里，于是**导入一个
纯字符串函数要先跑 `agent/__init__.py`** → `agent.agent` → `agent.graph`（9826 行 +
langgraph），实测冷启动 **1.69 秒**（其中 `agent.graph` 占 1.71s 累计）——离线工具
（`eval/recall_eval.py`、`eval/frame_budget.py`、任何只想要词法检索的入口）每次冷启动
都在为一件不相干的事付这笔钱，两处调用点还各自写了"惰性导入：别让 tools 层启动即拉
agent 包"来把它藏起来。`rag` 是三处里最低的一层（`agent/decisions.py`、`agent/context.py`、
`tools/base.py` 都已经在 import `rag.search`），所以中立落点选这里——**导入它不再拉起
任何东西**（本模块只 import `re`）。

为什么需要它（"超长文章"问题的正解）：

1. **渲染侧（agent/context.py `_frame_texts`）**：`get_article_detail` 的全文帧按字符
   上限硬截。实测站内最长文章 note 19 = 25,445 字，上限 20,000 意味着 §7-§10 四个
   整节**从来没进过上下文**，而模型连"有东西被截掉了、截的是什么"都不知道——它只
   看到一段在句子中间断掉的文章。现在的做法是**按小节取舍**：能装下的整节保留，
   装不下的整节以标题清单列在文末，并附"怎么取回"的说明。模型于是知道边界在哪。
2. **读取侧（tools/base.py `get_article_detail(section=…)`）**：知道边界之后必须有
   取回手段，否则"知道缺什么"只是更精确的无奈。`pick` 把用户的/planner 的小节指称
   （标题全称、编号"9"、唯一子串）解析成一个小节读回来。
3. **检索侧（rag/search.py `chunk_note`）**：BM25 索引本来就按同样的规则切节
   （20260901 起），但那是它自己实现的一份。两侧各写一份的代价是"改了一边忘了
   另一边"——索引切片、检索候选里的 `sections`、按节取回，三者必须同一套边界。

**纯函数**：只吃字符串、只吐字符串/字典，不碰 IO、不碰模型（tests/test_sections.py 据此
秒级断言，含 note 19 的真实形状）。

切分规则与 20260901 起的索引完全一致：只认 `#`/`##`/`###`（1-3 级），标题行本身
不进 `text`；首个标题之前的内容归到 `section=文章标题`（`level=0`）。**不要把
`#{1,3}` 放宽到 4-6 级**：索引侧的 chunk 边界一变，检索分数与 golden 全部跟着漂。

**父标题与它的子节是两个平级的 chunk**（`## 6. 防幻觉…` 与 `### 6.1 …` 都在列表里），
于是**父标题自己的 `text` 常常是空串**——内容全都住在子节里。实测线上语料 10 篇
**317 节里 47 节正文为空**（19 节的正文住在子节里、28 节是真空节；最长的一篇 25 节里 3 节，
另一篇 178 节里 28 节）。这不是
边角形状，是常态，两处调用点各有一条判据挡着：

- `pick` 命中一个空正文的父标题时，**连带把子节并进来**（见 `_with_children`）；
- `excerpt` 的未展开清单**不列空正文的节**——把读不出内容的节名报成"可以按它取回
  全文"是一句兑现不了的承诺，模型照它发一次按节读取只会空手而归（见 `excerpt`）。
"""

from __future__ import annotations

import re

# 只认 1-3 级（与索引一致，见模块头注）
_HEADING_RE = re.compile(r"^#{1,3}\s+(.+)$")

# 未展开小节清单的标记串：渲染侧靠它在帧注记里区分"按节节选"与"退回头截断"，
# tests/test_sections.py 也断言它出现。改字面量要同步 agent/context.py 的用法。
UNEXPANDED_MARK = "**以下小节尚未展开**"

# 清单区的预算（字符）：标题清单 + 取回说明。从节选上限里预留出来，保证
# excerpt() 的输出不越 cap（"不越上限"是它可以替换旧硬截断的前提）。
_OUTLINE_RESERVE = 600
_OUTLINE_MAX_ITEMS = 12


def split(content: str, title: str = "", shortcut: bool = True) -> list[dict]:
    """按 markdown 标题切节 → `[{"section", "text", "level"}]`。

    shortcut=True（默认，= 索引侧既有行为）：正文 < 2000 字不切，整篇算一节
    （小节名取文章标题）——短文的标题结构没有检索价值，切开只会把同一篇拆成
    多个低分 chunk。**渲染侧与取回侧要 shortcut=False**：那两处关心的是"哪一段
    属于哪一节"，与文章长短无关。
    """
    text = content or ""
    if shortcut and len(text) < 2000:
        return [{"section": title, "text": text, "level": 0}]
    chunks: list[dict] = []
    cur: list[str] = []
    cur_section, cur_level = title, 0
    for line in text.split("\n"):
        m = _HEADING_RE.match(line.strip())
        if m:
            if cur:
                chunks.append({"section": cur_section, "text": "\n".join(cur),
                               "level": cur_level})
            cur_section = m.group(1)
            cur_level = len(line.strip()) - len(line.strip().lstrip("#"))
            cur = []
        else:
            cur.append(line)
    if cur:
        chunks.append({"section": cur_section, "text": "\n".join(cur),
                       "level": cur_level})
    return chunks


def headings(content: str) -> list[str]:
    """正文里的小节名（不含 level=0 的开头段）——"这篇文章有哪些节"。"""
    return [c["section"] for c in split(content, shortcut=False) if c["level"]]


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", str(s or "")).strip().lower()


# 标题前导编号：`9. 部署与运维`→`9`；`3.1 时序总览`→`3.1`（多点也照收）。
_LEAD_NUM_RE = re.compile(r"^\s*(\d+(?:\.\d+)*)")


def _lead_num(s: str) -> str:
    m = _LEAD_NUM_RE.match(str(s or ""))
    return m.group(1) if m else ""


def _is_lossy(content: str, chunks: list[dict]) -> bool:
    """chunk 列表是不是**有损**的（正文里有标题行没能成为 chunk）。

    `split` 对"标题行**紧跟着另一个标题行**"的那一个不产 chunk（`cur == []`：
    标题前一行也是标题，中间一行正文都没有）。实测线上语料 10 篇里 15 处，
    **全部在同一篇的代码块里**（`# <<<<<<< HEAD`、`# drop = 删除该提交` …）。

    为什么这件事必须先判：**"谁是谁的子节"是靠层级推的**。丢掉的那个标题不在列表里，
    它后面的子节就会被算到**前一个**标题头上 ⇒ "读第 4 节"会把第 5 节的内容一起端出来，
    而且是**无声**的。有损时一律**不做任何层级推导**（只认自身正文），代价是那一篇
    拿不到并入的好处、退回"这一节没有正文"的如实分支——过度并比比读不到更坏。
    """
    heads = sum(1 for ln in (content or "").split("\n") if _HEADING_RE.match(ln.strip()))
    lead = 1 if chunks and not chunks[0].get("level") else 0   # 首个标题之前的正文
    return len(chunks) - lead != heads


def _child_span(chunks: list[dict], i: int, lossy: bool = False) -> tuple[int, int]:
    """第 i 个 chunk 的**子节区** `[起, 止)`：紧跟其后、层级更深的那一段。

    父标题与子节是平级 chunk（见模块头注），"属于这一节的还有什么"只能靠层级推；
    `lossy`（见 `_is_lossy`）时返回空区间——列表不完整时这层推导不成立。
    """
    if lossy:
        return i + 1, i + 1
    lvl = int(chunks[i].get("level") or 0)
    j = i + 1
    while j < len(chunks) and int(chunks[j].get("level") or 0) > lvl:
        j += 1
    return i + 1, j


def _is_readable(chunks: list[dict], i: int, lossy: bool = False) -> bool:
    """这一节发一次按节读取，能不能真的拿回正文。

    两种能：① 自身有正文；② 自身没正文但**子节有**（取回时会连子节一起给，见
    `_with_children`）。两者都不满足 = 真空节（线上确有：代码块里的 `# 注释` 行被
    当成标题，下一条注释行又把它截断 ⇒ 一串 0 字的"节"）。
    """
    start, end = _child_span(chunks, i, lossy)
    return bool((chunks[i].get("text") or "").strip()) or end > start


def readable_sections(content: str, title: str = "") -> list[str]:
    """真的读得出来的小节名（顺序同正文）——空节的候选清单该用这个，别用全部小节。"""
    chunks = split(content, title, shortcut=False)
    lossy = _is_lossy(content, chunks)
    return [c["section"] for i, c in enumerate(chunks)
            if c["level"] and _is_readable(chunks, i, lossy)]


def _with_children(chunks: list[dict], i: int, lossy: bool = False) -> dict:
    """命中那一节 → 自身正文为空时，**把子节的正文并进来**（返回里 `subsections` 记下并入了谁）。

    **只在自身正文为空时才并入**：父标题没有正文是常态（见模块头注，线上 317 节里
    47 节正文为空、其中 19 节的内容住在子节里），内容全都住在 `### 6.1 …` 里。只回父标题自己的 `text` 就是一个空串，
    而调用方是照着"命中"这件事说"读到了节选"的——模型拿到空帧、也没有任何下一步可走
    （现场：问"第 6 节写了什么"，读回来是空的，模型只能说读不到，重试一次还是同一格）。
    并入之后"读第 6 节"才等于人话里的读第 6 节。
    **自身有正文时正文一字不变**：那种形状下父标题自己就是要读的东西，把子节悄悄塞
    进来只会让每一帧无谓变长（此时 `subsections` 是空的——它记的是**并入了谁**，
    不是"有没有子节"，调用方据此说的那句话才不会失真）。
    """
    base = chunks[i]
    start, end = _child_span(chunks, i, lossy)
    if (base.get("text") or "").strip() or end <= start:
        return {**base, "subsections": []}
    kids = chunks[start:end]
    return {**base,
            "text": "\n\n".join(render(k) for k in kids),
            "subsections": [k["section"] for k in kids]}


def pick(content: str, want: str, title: str = "") -> dict | None:
    """按指称取一个小节；取不到/不唯一 → None（调用方据此回"可用小节"清单）。

    三级匹配，逐级放宽，**命中即返回**：
      ① 标题全称（归一化后相等）；
      ② 前导编号相等（"9" ↔ "9. 部署与运维"、"3.1" ↔ "3.1 时序总览"）；
      ③ 唯一子串（"部署" ↔ "9. 部署与运维"）——**唯一才认**：两节都含"部署"
         时返回 None，让调用方把候选列出来，而不是赌一个。
    不唯一时给候选清单比给一个可能错的小节好：模型照清单改一次指称即可（或直接
    走 ID/编号），而赌错一次就是"读了别的节还声称读了"。

    返回的 `text` 经 `_with_children` 过一道（空正文的父标题把子节并进来），
    `subsections` = **这次并入了哪些子节名**（没并入就是空列表——它记的是"正文里
    含了谁"，不是"有没有子节"）。调用方要用它把"这一节含哪些子节"如实说清楚，
    否则模型不知道子节的存在，只能拿 `section=` 一个名字一个名字地试。
    """
    w = _norm(want)
    if not w:
        return None
    all_chunks = split(content, title, shortcut=False)
    lossy = _is_lossy(content, all_chunks)          # 有损 ⇒ 不推层级（见 _is_lossy）
    chunks = [c for c in all_chunks if c["level"]]
    if not chunks:
        return None
    for i, c in enumerate(chunks):        # ① 全称
        if _norm(c["section"]) == w:
            return _with_children(chunks, i, lossy)
    for i, c in enumerate(chunks):        # ② 编号：want 是"9"或"9."都认
        if _lead_num(c["section"]) and _lead_num(c["section"]) == _lead_num(w):
            return _with_children(chunks, i, lossy)
    hits = [i for i, c in enumerate(chunks) if w in _norm(c["section"])]
    return _with_children(chunks, hits[0], lossy) if len(hits) == 1 else None


def candidates(content: str, want: str, title: str = "") -> list[str]:
    """`want` 的候选小节名（子串命中全部；无命中则返回全部节）——回给模型改指称。"""
    w = _norm(want)
    chunks = [c for c in split(content, title, shortcut=False) if c["level"]]
    hits = [c["section"] for c in chunks if w and w in _norm(c["section"])]
    return hits or [c["section"] for c in chunks]


def render(chunk: dict) -> str:
    """小节 → 带标题行的 markdown 片段（渲染侧要用，因为它比裸 text 好读）。"""
    if chunk.get("level"):
        return "#" * int(chunk["level"]) + " " + chunk["section"] + "\n" + chunk["text"]
    return chunk["text"]


def outline_text(dropped: list[str]) -> str:
    """未展开小节的清单 + 取回说明（模型据此发起一次按节读取）。"""
    shown = dropped[:_OUTLINE_MAX_ITEMS]
    lines = [UNEXPANDED_MARK + "：" + " / ".join(f"§{s}" for s in shown)]
    if len(dropped) > len(shown):
        lines.append(f"（另有 {len(dropped) - len(shown)} 节未列出）")
    lines.append(
        "（要读其中某一节：再调用一次 get_article_detail，带上本帧开头的 noteKey "
        "与 section=\"<上面的小节名或编号>\"，即可取回该节全文。）")
    return "\n".join(lines)


def excerpt(content: str, cap: int, title: str = "") -> str:
    """按小节节选正文，**整节取舍**，不越 `cap`。

    - 全文装得下 → 原样返回（99% 的文章走这条，行为与旧实现完全一致）；
    - 装不下 → 依次保留整节直到预算（`cap - _OUTLINE_RESERVE`），其余整节以标题
      清单落在文末（含取回说明）；**空正文的节不进清单**——它读不出内容，列上去
      等于指一条死路（它的子节另有自己的条目，不会因此漏掉）；
    - 只有一节（正文没有小节结构）或首节自己就超预算 → 退回**头截断**并标注，
      此时至少要说清"截了"（旧实现的问题不是截断本身，而是截断无声）。

    `title` 是文章标题（首段的归属节名），只用于切分，不出现在输出里。
    """
    text = content or ""
    if len(text) <= cap:
        return text
    chunks = split(text, title, shortcut=False)
    if len(chunks) <= 1:
        return _head_cut(text, cap, "全文无小节结构")
    lossy = _is_lossy(text, chunks)
    budget = max(0, cap - _OUTLINE_RESERVE)
    kept: list[str] = []
    dropped: list[int] = []
    used = 0
    for i, c in enumerate(chunks):
        piece = render(c)
        if used + len(piece) <= budget or not kept:   # 首节无论多大都保留
            kept.append(piece)
            used += len(piece)
        else:
            dropped.append(i)
    # 清单里只列**真的读得出来**的节（`_is_readable`：自身有正文，或正文在子节里）。
    # 列一个空节就是一句兑现不了的承诺——模型照它发一次按节读取，拿回来的是"这一节
    # 没有正文"，白花一轮；而它的子节本来就另有自己的条目，不会因此漏掉。
    # 清单被滤成空时退回原名（宁可列空的，也不静默少说一句"还有多少节没展开"）。
    listed = [chunks[i]["section"] for i in dropped
              if _is_readable(chunks, i, lossy)]
    listed = listed or [chunks[i]["section"] for i in dropped]
    if not dropped:                    # 预算恰好装下（走上面的 len<=cap 分支才正常）
        return text[:cap]
    out = "\n\n".join(kept)
    tail = "\n\n" + outline_text(listed)
    room = cap - len(out)
    if len(tail) <= room:
        return out + tail
    compact = ("\n\n" + UNEXPANDED_MARK + "："
               + " / ".join(f"§{s}" for s in listed[:_OUTLINE_MAX_ITEMS]))
    if len(compact) <= room:           # 放不下取回说明时，至少把"缺了哪几节"留下
        return out + compact
    # 预算被首节吃光，连清单都放不下：退回头截断（宁可说"截了"，也不静默丢节）
    return _head_cut(text, cap, "首个超过单帧上限的小节独占全文")


def _head_cut(text: str, cap: int, why: str) -> str:
    """头截断兜底：截断本身可以接受，**无声**截断不行——所以带上总长与成因。"""
    head = text[:max(0, cap - 120)]
    tail = (f"\n\n…（原文共 {len(text)} 字，此处为前 {len(head)} 字；{why}，"
            f"未能按小节列出未展开的部分——需要后续内容请按小节名重试调用）")
    return head + tail


# ── 工具帧 → 可读节选（渲染侧的入口）────────────────────────────────

_HEADER_VALUE_MAX = 200      # 帧头里每个非正文字段保留的长度
_HEADER_KEYS_MAX = 12


def _unescape(text: str) -> str:
    """repr 里换行是**字面** `\\n`（两个字符）——按 `^#{1,3}` 切之前必须先还原，
    否则一节都切不出来（实测：note 19 的详情 repr 52834 字里真换行 0 个、
    字面 `\\n` 1154 个）。"""
    return text.replace("\\n", "\n")


def slim_frame(frame: str) -> str:
    """详情执行帧 → 去掉**同文本的重复正文键**（帧体积接近减半）。

    `frame_excerpt` 注释里那个坑②（详情 dict 同时带 `noteContent` 与 `content`）此前
    只在**渲染**时被吸收——那只救了 planner 看的那一份（`_frame_texts`）；narrator 拿的是
    **原始 ToolMessage**（`graph.model_node` 的 `[system] + state["messages"]`），重复原样
    进提示词：note 19 的帧 52,834 字里有一半是同一段正文（实测正文 25,445 字）。
    20260925 把去重**前移到造帧那一刻**（`execute_node` 的 ToolMessage 出口），两个键都省。

    三条边界：
      · 两个键必须**字节相等**才删（不等 ⇒ 内容真不同，一个都不动）；
      · 判据不认识的帧**原样返回**——帧的形态不止一种（`__ERROR__` 帧、命令帧、列表帧），
        本函数对它们必须是恒等变换；
      · **只动帧**：`tool_data` 仍由 `parse_data(str(out))` 从**原始**字符串解析
        ⇒ `$tool[N].content` 之类的参数引用取值不受影响（**不许**改 `tools/base.py` 的
        `_shape`，那会同时改掉引用取值源）。
    删键后仍是合法 dict repr ⇒ `_split_frame` 的 `literal_eval` 路径照旧。
    """
    if "'noteContent'" not in frame and '"noteContent"' not in frame:
        return frame                     # 快路：绝大多数帧没有这个键，不必 literal_eval
    import ast
    try:
        data = ast.literal_eval(frame)
    except Exception:
        return frame
    if not isinstance(data, dict):
        return frame
    body = data.get("noteContent")
    if not isinstance(body, str) or not body:
        return frame
    dup = [k for k, v in data.items() if k != "noteContent" and v == body]
    if not dup:
        return frame
    for k in dup:
        data.pop(k)
    return str(data)


def frame_excerpt(frame: str, cap: int) -> str:
    """get_article_detail 的执行帧 → 提示词里可读的按节节选。

    帧是 `str(dict)`（见 tools/base.py `_shape`），直接硬截有两个坑（都实测于 note 19）：
      ① 换行是字面 `\\n`，按 `^#{1,3}` 切节会切出 0 节（= 小节清单永远列不出来）；
      ② 详情 dict **同时带 `noteContent` 与 `content` 两个同文本键**——帧体积是正文
         的两倍，20,000 字的预算实际只装得回半篇（这就是 §7-§10 从来没进过上下文的
         字面原因）。
    所以先 `literal_eval` 还原结构：非正文字段（noteKey/noteTitle/description…）
    压成一行帧头，正文取最长值那个键、按节节选。解析不出来（不是 dict repr）就退回
    把字面 `\\n` 还原后按文本处理——无论如何都不会退化成"无声截断"。
    """
    body, header = _split_frame(frame)
    if body is None:
        return excerpt(_unescape(frame), cap)
    head_txt = header + "\n\n正文：\n" if header else ""
    return head_txt + excerpt(body, max(200, cap - len(head_txt)))


def _split_frame(frame: str) -> tuple[str | None, str]:
    """帧 → (正文, 帧头文本)。不是"含正文的 dict repr"时正文为 None。"""
    import ast
    try:
        data = ast.literal_eval(frame)
    except Exception:
        return None, ""
    if not isinstance(data, dict):
        return None, ""
    str_vals = {k: v for k, v in data.items() if isinstance(v, str)}
    # 正文 = 最长的字符串字段（noteContent/content/description 三者里正文明摆着最长；
    # 空正文的短文档也不会走到本函数——调用方只在超过上限时调）
    if not str_vals:
        return None, ""
    body_key = max(str_vals, key=lambda k: len(str_vals[k]))
    body = str_vals[body_key]
    if len(body) < 500:
        return None, ""
    head: dict = {}
    for k, v in data.items():
        if k == body_key:
            continue
        # 正文的重复副本也丢掉：详情 dict 里 `noteContent` 与 `content` 是同文本，
        # 只丢一份、另一份照样截 200 字放帧头等于白占位置（帧预算就是正文预算）。
        if isinstance(v, str) and len(v) > _HEADER_VALUE_MAX and v[:40] == body[:40]:
            continue
        head[k] = v
    items = list(head.items())[:_HEADER_KEYS_MAX]
    head_txt = "{" + ", ".join(
        f"{k!r}: {v!r}" if not isinstance(v, str) or len(v) <= _HEADER_VALUE_MAX
        else f"{k!r}: {v[: _HEADER_VALUE_MAX]!r}…"
        for k, v in items) + "}"
    return body, head_txt
