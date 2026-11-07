# -*- coding: utf-8 -*-
"""Public article, taxonomy and activity reads with bounded source evidence."""

import logging
from typing import Annotated, Literal

from langchain_core.tools import tool

from agent.domains.article_metadata import tag_references
from tools import base as _base
from tools.result import (
    ToolResult,
    _shape,
    empty,
    not_found,
    ok,
    unavailable,
)

# Public exports; helper names stay private to their modules.
__all__ = ["logger", "search_notes", "DOC_TYPE_CN", "get_article_detail", "rag_search",
           "get_blog_info", "get_social_links"]

logger = logging.getLogger(__name__)


# Resolve shared transports after module initialization.
def _get(path: str, *, not_found_text: str = "",
         payload: dict | None = None) -> dict | list | ToolResult:
    """Resolve the shared public transport at call time, preserving its result type."""
    return _base._get(path, not_found_text=not_found_text, payload=payload)


_NOTE_COUNT_KEYS = ("views", "likes", "favorites")


def _note_counts(row) -> dict:
    """Keep observed nonnegative integer counters; malformed values remain unknown."""
    out = {}
    for k in _NOTE_COUNT_KEYS:
        v = row.get(k)
        if type(v) is int and v >= 0:
            out[k] = v
    return out


def _note_tag_metadata(row):
    """Missing or invalid host references remain unknown, distinct from no tags."""
    try:
        refs = tag_references(row.get("noteTagRefs"))
    except ValueError:
        return {"tags": None}
    return {"tags": refs, "tagsTruncated": len(row["noteTagRefs"]) > len(refs)}


def _slim_note_rows(rows):
    """note 行数组 → 精简行数组（**认不出形态返回 None**，调用方原样透出）。

    只对"看得出是 note 行"的数组动手（行里有 `noteKey`）：其它工具的列表
    （留言/说说/设备…）字段完全不同，套同一把刀会砍掉它们的正文。
    """
    if not isinstance(rows, list) or not rows or not all(isinstance(r, dict) for r in rows):
        return None
    if not any("noteKey" in r for r in rows):
        return None
    out = []
    for r in rows:
        slim = {
            "noteKey": r.get("noteKey"),
            "noteTitle": r.get("noteTitle") or "",
            "status": r.get("status") or "",
            "isTop": r.get("isTop") or 0,
            **_note_tag_metadata(r),
        }
        slim.update(_note_counts(r))
        out.append(slim)
    return out


@tool
def search_notes(keyword: Annotated[str, "搜索关键词"]) -> str | ToolResult:
    """按关键词匹配文章标题、正文和标签，返回按相关度排序的文章元数据。

    按标题或标签查找文章用本工具，按内容主题检索可用 rag_search。
    候选标题和链接可直接用于推荐；解读、总结或比较正文时需用 get_article_detail 读取正文。
    """
    data = _get("/notes/search", payload={"keyword": keyword})
    if isinstance(data, ToolResult):
        return data
    # Successful empty results remain distinct from transport failures.
    if not data:
        return empty("[]")
    slim = _slim_note_rows(data)
    return _shape(slim if slim is not None else data)


def _note_with_tag_refs(row):
    """Use the host's typed references without fetching taxonomy directories."""
    out = {**row, **_note_tag_metadata(row)}
    out.pop("noteTagRefs", None)
    out.pop("noteTags", None)
    return out


def _note_public_counts(article_id) -> dict:
    """Read non-incrementing public article counters.

    Private or unavailable statistics return no fields, never fabricated zeros.
    The actor-specific liked flag is omitted because this is a public service read."""
    data = _get(f"/notes/{article_id}/stats")
    if not isinstance(data, dict):
        return {}
    return _note_counts(data)


# Document labels must stay aligned with host execution-receipt rendering.
DOC_TYPE_CN = {
    "note": "文章",
    "talk": "说说",
    "board": "留言",
    "announcement": "公告",
}


_ARTICLE_OFFSET_DESC = "正文范围起点；用返回中的 nextOffset 继续读取"
_ARTICLE_LIMIT_DESC = "本次最多读取字符数，1..24000，默认 12000；实际可见范围受剩余上下文预算限制"
_ARTICLE_REVISION_DESC = "继续读取时照抄 contentRevision，防止跨版本拼接"


@tool
def get_article_detail(
    article_id: Annotated[
        int,
        "文档的唯一 ID（note 为 noteId，talk/board 为 talkId，announcement 为 id）",
    ],
    doc_type: Annotated[
        Literal["note", "talk", "board", "announcement"],
        "文档类型：note（文章，默认）/ talk（说说）/ board（留言）/ announcement（公告）",
    ] = "note",
    section: Annotated[
        str,
        "只读该文章的某一小节：优先用检索的 section id 并带 revision；支持唯一标题/编号",
    ] = "",
    offset: Annotated[int, _ARTICLE_OFFSET_DESC] = 0,
    max_chars: Annotated[int, _ARTICLE_LIMIT_DESC] = 12000,
    revision: Annotated[str, _ARTICLE_REVISION_DESC] = "",
) -> str | ToolResult:
    """读取指定来源的正文；文章按小节或范围续读，使用contentRevision和nextOffset。

    readComplete表示所需范围读完；候选不是正文。编号按doc_type区分，tags包含层级与名称，
    null表示未能核对标签；缺失计数不代表零，not_found不代表服务故障。
    """
    if doc_type == "note":
        data = _get(f"/notes/{article_id}", not_found_text=(
            f"站内没有 id={article_id} 这篇文章，本次**没有**读到任何正文。"
            "另注意 id 不是同一套：通知/留言板里的 id 是留言 id，不是文章 id。"
            "按标题找文章用 search_notes 或 rag_search；"
            "读留言/说说用 list_guestbook / list_talks。"))
        if isinstance(data, dict):
            data = _note_with_tag_refs(data)
            counts = _note_public_counts(article_id)
            if counts:
                data = {**data, **counts}
        if not isinstance(data, dict):
            return _shape(data)
        from rag.reading import RevisionChanged, article_page
        from tools.result import invalid_args
        try:
            return _shape(article_page(data, article_id, section, offset, max_chars, revision))
        except RevisionChanged as exc:
            from rag.search import invalidate_document
            invalidate_document(article_id, revision)
            return unavailable(str(exc))
        except ValueError as exc:
            return invalid_args(str(exc))
    endpoint, key_field = {
        "talk": ("/talk", "talkKey"),
        "board": ("/board", "talkKey"),
        "announcement": ("/announcements", "id"),
    }[doc_type]
    rows = _get(endpoint)
    if not isinstance(rows, list):
        return rows
    for it in rows:
        if str(it.get(key_field)) == str(article_id):
            return _shape(it)
    # 公开列表有范围及审核过滤，缺席不能证明全站不存在。
    return not_found(
        f"公开列表里没有 {key_field}={article_id} 这一条（河灯留言只放行审核通过的，"
        "待审与被驳回的不在列表里；列表也可能只回最近若干条）。所以这是『我没找到』，"
        "不是『站内一定没有』。另注意 id 不是同一套：通知链接里的 lid 是留言 id，不是文章 id。")


def _rag_hit(index, hit):
    title = hit["title"][:24].replace("\n", " ").replace("\r", " ")
    text = f"{index}. type={hit['type']} id={hit['id']} rank={index} title={title}"
    hints = []
    for position, label in enumerate((hit.get("sections") or [])[:3]):
        label = label[:80].replace("\n", " ").replace("\r", " ")
        hint = f"命中节={label}"
        identifiers = hit.get("section_ids") or []
        if position < len(identifiers) and hit.get("document_revision"):
            ident = identifiers[position]
            match = next((m for m in hit.get("matches", []) if m.get("section_id") == ident), {})
            hint += f" section={ident} revision={hit['document_revision']}"
            offset = match.get("offset")
            if type(offset) is int and offset >= 0:
                hint += f" offset={offset}"
        hints.append(hint)
    return text + (" " + " | ".join(hints) if hints else "")


@tool
def rag_search(
    query: Annotated[str, "检索关键词（用户问题中希望从博客内容里找到答案的核心表述）"],
    top_k: Annotated[int, "返回候选文档数量，默认 8"] = 8,
) -> str | ToolResult:
    """按主题检索公开文章候选与命中小节，不返回全文。

    推荐可用候选链接；解读需用get_article_detail按命中section及revision补读。
    公告、留言和说说的关键词查询使用public_activity领域，不在此语料中。
    """
    try:
        from rag.search import last_route, search
        hits = search(query, top_k=top_k)
        if hits is None:
            return unavailable("检索索引未就绪（语料为空或重建失败），暂时无法检索")
        if not hits:
            return empty("检索无结果")
        text = "\n".join(_rag_hit(i + 1, hit) for i, hit in enumerate(hits))
        # Retrieval mode belongs to telemetry; candidate format is shared by both routes.
        route = last_route()
        from rag.search import get_index
        corpus = get_index().status()
        if corpus["stale"]:
            text += ("\n注：语料刷新未完成，本次候选使用最近一次完整索引；"
                     "正文读取仍核对当前公开版本。")
        return ok(text, data=hits, meta={"corpus": corpus, "rag_mode": route["mode"],
                              "rag_reason": route["reason"],      # degraded 时的原因码
                              "vector_hits": route["vectors"],    # 向量路贡献了几条候选
                              "vector_missing": route["missing"]})
    except Exception as exc:
        logger.error("rag_search failed: %s", exc)
        return unavailable(f"检索服务不可用（{type(exc).__name__}）")


# Category and tag reads.


# Announcements.


# Guestbook reads.


# Public reads have no actor identity, so their mine flag is not evidence of ownership.
# Use the authenticated own-message endpoint for ownership queries.


# Talks and activity.


# Site information.

@tool
def get_blog_info() -> str | ToolResult:
    """获取博客基本信息：作者、头像、签名、ICP备案号等。"""
    data = _get("/user")
    return _shape(data)

@tool
def get_social_links() -> str | ToolResult:
    """获取社交链接（QQ、GitHub、BILIBILI等）。"""
    data = _get("/social")
    return _shape(data)
