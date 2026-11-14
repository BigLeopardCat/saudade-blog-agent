# -*- coding: utf-8 -*-
"""Pure Markdown section boundaries shared by indexing, range reads and excerpts.

CommonMark top-level headings delimit sections; markdown-it-py handles syntax.
The source-backed outline preserves
parent paths, repeated titles and empty parents; split supplies the compact
section view. Changing these boundaries requires rebuilding the retrieval index.

Unexpanded content is identified explicitly instead of silently truncated.
Empty parents may include their descendants only when the heading structure
is complete; ambiguous references never select an arbitrary section."""

from __future__ import annotations

import re
from dataclasses import dataclass
from importlib.metadata import version

from markdown_it import MarkdownIt

PARSER_VERSION = "commonmark-top-level-source-v1"
_MARKDOWN = MarkdownIt("commonmark").disable("inline")


def parser_signature() -> str:
    return f"{PARSER_VERSION}:markdown-it-py-{version('markdown-it-py')}"


def body_blocks(text: str) -> list[tuple[int, int]]:
    """Top-level CommonMark block ranges, including intervening source whitespace."""
    offsets = [0, *(m.end() for m in re.finditer(r"\r\n|\r|\n", text)), len(text)]
    starts = {0, len(text)}
    for token in _MARKDOWN.parse(text):
        if token.level == 0 and token.map is not None:
            starts.add(offsets[token.map[0]])
    positions = sorted(starts)
    return list(zip(positions, positions[1:]))


def reading_revision(title: str, content: str) -> str:
    """Canonical source content hash; section parser identity travels separately."""
    from rag.corpus import content_revision

    return content_revision(title, content)


@dataclass(frozen=True)
class Heading:
    start: int
    end: int
    title: str
    level: int


# Excerpt consumers identify the unexpanded outline through this marker.
UNEXPANDED_MARK = "**以下小节尚未展开**"

# Reserve space for the heading list and continuation instructions within the excerpt cap.
_OUTLINE_RESERVE = 600
_OUTLINE_MAX_ITEMS = 12


def _heading_hits(content: str) -> list[Heading]:
    # CommonMark normalizes CR/LF internally. Project its line maps onto the
    # original string, including CRLF and Unicode; never search rendered text.
    offsets = [0, *(match.end() for match in re.finditer(r"\r\n|\r|\n", content)), len(content)]
    tokens = _MARKDOWN.parse(content)
    result = []
    for i, token in enumerate(tokens):
        # Quoted/list-local headings stay inside their containing article body.
        if token.type != "heading_open" or token.level != 0 or token.map is None:
            continue
        start, end = token.map
        result.append(Heading(
            offsets[start], offsets[end], tokens[i + 1].content, int(token.tag[1:]),
        ))
    return result


def outline(content: str, title: str = "") -> list[dict]:
    """Source-backed sections, preserving empty parents and duplicate headings."""
    text = content or ""
    headings = _heading_hits(text)
    records: list[dict] = []
    stack: list[tuple[int, str]] = []

    def append(section: str, level: int, low: int, high: int, heading: str = "") -> None:
        records.append({
            "section": section, "text": text[low:high], "level": level,
            "path": [name for _, name in stack], "section_id": f"s{len(records) + 1}",
            "body_start": low, "body_end": high, "heading": heading,
        })

    if not headings:
        append(title, 0, 0, len(text))
        return records
    if headings[0].start:
        append(title, 0, 0, headings[0].start)
    for i, heading in enumerate(headings):
        while stack and stack[-1][0] >= heading.level:
            stack.pop()
        stack.append((heading.level, heading.title))
        end = headings[i + 1].start if i + 1 < len(headings) else len(text)
        append(heading.title, heading.level, heading.end, end, text[heading.start:heading.end])
    return records


def split(content: str, title: str = "", shortcut: bool = True) -> list[dict]:
    """Compact excerpt view; indexing and range reads use the full source outline."""
    text = content or ""
    if shortcut and len(text) < 2000:
        return [{"section": title, "text": text, "level": 0}]
    rows = outline(text, title)
    compact = []
    for i, row in enumerate(rows):
        # Empty parents remain in the source outline but add no text to compact excerpts.
        if not row["text"]:
            continue
        body = row["text"]
        if i < len(rows) - 1:
            if body.endswith("\r\n"):
                body = body[:-2]
            else:
                body = body.removesuffix("\n").removesuffix("\r")
        compact.append({**row, "text": body})
    return compact


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
    heads = len(_heading_hits(content or ""))
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
    if chunk.get("heading"):
        return chunk["heading"] + chunk["text"]
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
        "与 selection={kind=\"source_range\", section=\"<上面的小节名或编号>\"}；"
        "编号小节还须回传 contentRevision 与 sectionSchema。）")
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


def slim_frame(data: dict | str) -> str:
    """Render structured data once, removing only byte-identical duplicate body fields."""
    if not isinstance(data, dict):
        return data
    body = data.get("noteContent")
    if not isinstance(body, str) or not body:
        return str(data)
    return str({key: value for key, value in data.items()
                if key != "content" or value != body})


def frame_excerpt(frame: dict | str, cap: int) -> str:
    """Render structured metadata and a bounded excerpt without parsing display text."""
    body, header = _split_frame(frame)
    if body is None:
        return excerpt(str(frame), cap)
    head_txt = header + "\n\n正文：\n" if header else ""
    return head_txt + excerpt(body, max(200, cap - len(head_txt)))


def _split_frame(data: dict | str) -> tuple[str | None, str]:
    """Select the body and metadata from structured source data."""
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
