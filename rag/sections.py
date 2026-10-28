# -*- coding: utf-8 -*-
"""Pure Markdown section boundaries shared by indexing, range reads and excerpts.

Only headings outside fenced code blocks delimit sections. outline preserves
parent paths, repeated titles and empty parents; split supplies the compact
section view. Changing these boundaries requires rebuilding the retrieval index.

Unexpanded content is identified explicitly instead of silently truncated.
Empty parents may include their descendants only when the heading structure
is complete; ambiguous references never select an arbitrary section."""

from __future__ import annotations

import re

# 围栏外的 1–6 级标题；索引与读取共用。
_HEADING_RE = re.compile(r"^#{1,6}\s+(.+)$")

# 围栏代码块（``` / ~~~，三个起；开栏行后面可以跟信息串，如 ```gitignore）
_FENCE_OPEN_RE = re.compile(r"^[ \t]{0,3}(`{3,}|~{3,})")

# 未展开小节清单的标记串：渲染侧靠它在帧注记里区分"按节节选"与"退回头截断"，
# Excerpt consumers and section tests share this marker.
UNEXPANDED_MARK = "**以下小节尚未展开**"

# 清单区的预算（字符）：标题清单 + 取回说明。从节选上限里预留出来，保证
# excerpt() 的输出不越 cap（"不越上限"是它可以替换旧硬截断的前提）。
_OUTLINE_RESERVE = 600
_OUTLINE_MAX_ITEMS = 12


def _fence_open(line: str) -> str:
    """这一行是不是围栏的**开栏行**；是就返回那串围栏（如 "```" / "~~~~"），否则 ""。"""
    m = _FENCE_OPEN_RE.match(line)
    return m.group(1) if m else ""


def _fence_close(line: str, fence: str) -> bool:
    """这一行关掉 `fence` 吗——**只由同一种字符构成、且不短于开栏那串**。"""
    s = line.strip()
    return bool(s) and set(s) == {fence[0]} and len(s) >= len(fence)


def _fenced_flags(lines: list[str]) -> list[bool]:
    """逐行标出"这一行住在围栏代码块里吗"（**围栏行自己也算 True**）。

    为什么必须有它：`.gitignore` / shell / diff 的**示例内容整段住在 ``` 围栏里**，
    而 `.gitignore` 的注释行就是 `# …` —— 与 markdown 一级标题**逐字同形**。不认围栏
    时它们会被当成标题切成几十个 0 字小节（线上 note:16 实测：从
    `# ========== 依赖管理 ==========` 一路切到 `# ========== 本地数据库文件 ==========`，
    11 节全部空正文，节名还是 gitignore 的注释）。围栏里的行一律不当标题。
    """
    flags: list[bool] = []
    fence = ""
    for ln in lines:
        if fence:
            flags.append(True)
            if _fence_close(ln, fence):
                fence = ""
            continue
        fence = _fence_open(ln)
        flags.append(bool(fence))
    return flags


def _heading_hits(lines: list[str], fenced: list[bool]) -> list[tuple[int, re.Match]]:
    """`(行号, 匹配)` 列表 —— 只认**围栏外**的 1–6 级标题。

    `split` 与 `_is_lossy` **共用这一把尺子**：两处各判一次的话，"正文里有几个标题行"
    与"切出几个 chunk"就按两套口径算，`_is_lossy` 会开始报假有损（或漏报真损）。
    """
    return [(i, m) for i, ln in enumerate(lines) if not fenced[i]
            and (m := _HEADING_RE.match(ln.strip()))]


def split(content: str, title: str = "", shortcut: bool = True) -> list[dict]:
    """按 markdown 标题切节 → `[{"section", "text", "level"}]`。

    shortcut=True（兼容旧渲染调用）：正文 < 2000 字不切，整篇算一节
    （小节名取文章标题）——短文的标题结构没有检索价值，切开只会把同一篇拆成
    多个低分 chunk。**渲染侧与取回侧要 shortcut=False**：那两处关心的是"哪一段
    属于哪一节"，与文章长短无关。

    围栏代码块里的行不当标题；索引侧使用保留路径与空父节的 outline。
    """
    text = content or ""
    if shortcut and len(text) < 2000:
        return [{"section": title, "text": text, "level": 0}]
    lines = text.split("\n")
    heads = dict(_heading_hits(lines, _fenced_flags(lines)))
    chunks: list[dict] = []
    cur: list[str] = []
    cur_section, cur_level = title, 0
    for i, line in enumerate(lines):
        m = heads.get(i)
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


def outline(content: str, title: str = "") -> list[dict]:
    """Lossless section records, including empty parents and duplicate headings.

    IDs are scoped to this document revision. All consumers use the same fenced
    Markdown boundaries; paths preserve the context of otherwise empty parents.
    """
    lines = (content or "").split("\n")
    heads = dict(_heading_hits(lines, _fenced_flags(lines)))
    records: list[dict] = []
    stack: list[tuple[int, str]] = []
    body: list[str] = []
    section, level = title, 0
    path: list[str] = []

    def append() -> None:
        records.append({"section": section, "text": "\n".join(body), "level": level,
                        "path": list(path), "section_id": f"s{len(records) + 1}"})

    for i, line in enumerate(lines):
        if i not in heads:
            body.append(line)
            continue
        if body or level:
            append()
        section = heads[i].group(1)
        level = len(line.strip()) - len(line.strip().lstrip("#"))
        while stack and stack[-1][0] >= level:
            stack.pop()
        stack.append((level, section))
        path = [name for _, name in stack]
        body = []
    if body or level:
        append()
    return records


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
    """Detect headings omitted from the compact section list.

    Hierarchy inference is unsafe when headings are missing: later descendants
    could attach to the wrong parent. Use the same heading parser as split."""
    lines = (content or "").split("\n")
    heads = len(_heading_hits(lines, _fenced_flags(lines)))
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
    """A section is readable when it has text or readable descendant text."""
    start, end = _child_span(chunks, i, lossy)
    return bool((chunks[i].get("text") or "").strip()) or end > start


def readable_sections(content: str, title: str = "") -> list[str]:
    """真的读得出来的小节名（顺序同正文）——空节的候选清单该用这个，别用全部小节。"""
    chunks = split(content, title, shortcut=False)
    lossy = _is_lossy(content, chunks)
    return [c["section"] for i, c in enumerate(chunks)
            if c["level"] and _is_readable(chunks, i, lossy)]


def _with_children(chunks: list[dict], i: int, lossy: bool = False) -> dict:
    """Include descendants only when the selected parent has no body.

    Never expand a nonempty parent implicitly. subsections records only the
    children whose text was actually included."""
    base = chunks[i]
    start, end = _child_span(chunks, i, lossy)
    if (base.get("text") or "").strip() or end <= start:
        return {**base, "subsections": []}
    kids = chunks[start:end]
    return {**base,
            "text": "\n\n".join(render(k) for k in kids),
            "subsections": [k["section"] for k in kids]}


def pick(content: str, want: str, title: str = "") -> dict | None:
    """Resolve a unique section by full title, leading number, then substring.

    Missing or ambiguous references return None. Empty parents may include
    descendant text through _with_children; subsections records that expansion."""
    w = _norm(want)
    if not w:
        return None
    all_chunks = split(content, title, shortcut=False)
    lossy = _is_lossy(content, all_chunks)          # 有损 ⇒ 不推层级（见 _is_lossy）
    chunks = [c for c in all_chunks if c["level"]]
    if not chunks:
        return None
    for matches in (
        [i for i, c in enumerate(chunks) if _norm(c["section"]) == w],
        [i for i, c in enumerate(chunks) if _lead_num(w)
         and _lead_num(c["section"]) == _lead_num(w)],
    ):
        if matches:
            return _with_children(chunks, matches[0], lossy) if len(matches) == 1 else None
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
    """Fit whole sections within cap and list omitted readable headings.

    If no whole section fits, return a labeled prefix. title names the leading
    preamble for splitting and does not appear in the rendered output."""
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
    # Only list readable sections. Keep a fallback outline if filtering removes every entry.
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
    """Remove only byte-identical duplicate body fields from a dictionary frame.

    Unknown frame shapes pass through unchanged. This transforms presentation
    only; the original structured tool data remains the source for consumers."""
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
    """Render a dictionary frame as metadata and a bounded section excerpt.

    Parse repr first so escaped newlines and duplicate body aliases do not consume
    the budget twice. Unknown shapes use the labeled plain-text excerpt path."""
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
