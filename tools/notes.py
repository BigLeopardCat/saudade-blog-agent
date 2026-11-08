# -*- coding: utf-8 -*-
"""Public article, taxonomy and activity reads with bounded source evidence."""

import json
import logging
from typing import Annotated, Literal

from langchain_core.tools import tool
from pydantic import Field, StrictInt

from agent.domains.article_metadata import tag_references
from agent.reports import sanitize_untrusted
from rag import DEFAULT_TOP_K
from tools import base as _base
from tools.result import (
    ToolResult,
    _shape,
    empty,
    invalid_args,
    ok,
    unavailable,
)

# Public exports; helper names stay private to their modules.
__all__ = [
    "logger",
    "search_notes",
    "DOC_TYPE_CN",
    "get_article_detail",
    "get_article_stats",
    "rag_search",
    "get_blog_info",
    "get_social_links",
]

logger = logging.getLogger(__name__)


# Resolve shared transports after module initialization.
def _get(
    path: str, *, not_found_text: str = "", payload: dict | None = None
) -> dict | list | ToolResult:
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
    """Project article metadata only; unknown shapes cannot establish search results."""
    if not isinstance(rows, list) or any(
            not isinstance(row, dict) or type(row.get("noteKey")) is not int
            or row["noteKey"] <= 0 for row in rows):
        return None
    out = []
    for r in rows:
        title = r.get("noteTitle")
        status, pinned = r.get("status"), r.get("isTop")
        slim = {
            "noteKey": r.get("noteKey"),
            "noteTitle": sanitize_untrusted(title, 255) if isinstance(title, str) else None,
            "titleTruncated": len(title) > 255 if isinstance(title, str) else None,
            "status": status if isinstance(status, str) else None,
            "isTop": pinned if type(pinned) is int and pinned in (0, 1) else None,
            **_note_tag_metadata(r),
        }
        slim.update(_note_counts(r))
        out.append(slim)
    return out


@tool
def search_notes(keyword: Annotated[str, "搜索关键词"]) -> str | ToolResult:
    """按关键词匹配文章标题、正文和标签，返回按相关度排序的文章元数据。

    按标题或标签查找文章用本工具，按内容主题检索可用 rag_search。
    返回本次查询范围和候选元数据；未命中仅表示本次公开文章关键词查询没有匹配，
    不证明其他主题、其他查询或非公开文章不存在。解读正文需用 get_article_detail。
    """
    if not keyword.strip():
        return invalid_args("搜索关键词不能为空；浏览公开文章请使用文章目录。")
    data = _get("/notes/search", payload={"keyword": keyword})
    if isinstance(data, ToolResult):
        return data
    slim = _slim_note_rows(data)
    if slim is None:
        return unavailable("文章搜索未返回可核对的结果，本次不能判断是否有匹配。")
    value = {"resource": "articles", "scope": "public_articles_keyword_matches",
             "query": keyword, "matchFields": ["title", "body", "tags"],
             "items": slim, "returnedCount": len(slim)}
    text = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return ok(text, data=value) if slim else empty(text, data=value)


# Document labels must stay aligned with host execution-receipt rendering.
DOC_TYPE_CN = {
    "note": "文章",
    "talk": "说说",
    "board": "留言",
    "announcement": "公告",
}


_ARTICLE_OFFSET_DESC = "正文范围起点；照抄nextOffset并保留revision/section_schema续读"
_ARTICLE_LIMIT_DESC = "本次最多读取字符数，1..24000，默认12000"
_ARTICLE_REVISION_DESC = "照抄contentRevision；续读和小节ID读取必须带版本"


@tool
def get_article_detail(
    article_id: Annotated[StrictInt, Field(gt=0, le=2_147_483_647)],
    doc_type: Literal["note", "talk", "board", "announcement"] = "note",
    section: Annotated[str, Field(max_length=1024)] = "",
    offset: Annotated[
        StrictInt, Field(ge=0, le=2_147_483_647, description=_ARTICLE_OFFSET_DESC)
    ] = 0,
    max_chars: Annotated[StrictInt, Field(ge=1, le=24000, description=_ARTICLE_LIMIT_DESC)] = 12000,
    revision: Annotated[str, Field(max_length=64, description=_ARTICLE_REVISION_DESC)] = "",
    section_schema: Annotated[str, Field(max_length=128)] = "",
) -> str | ToolResult:
    """Read bounded ORIGINAL public text by source type/ID, optionally a unique section.
    Copy contentRevision AND sectionSchema for continuation or a section ID from search.
    nextOffset continues the same selected scope; readComplete does not mean the whole
    article when a section was selected. Pictures remain original Markdown references.
    Public author/summary/cover fields are included; metadata-only reads may use max_chars=1.
    Private/draft articles and unapproved messages are unavailable here; open editor drafts
    use administrator read_current_article. Metrics use get_article_stats, not body reads.
    """
    from urllib.parse import urlencode

    from agent.domains.articles.reading import project
    from tools.result import invalid_args

    if (offset or section.startswith("s") and section[1:].isdigit()) and (
        not revision or not section_schema
    ):
        return invalid_args("Continuation requires contentRevision and sectionSchema.")
    params = {
        "offset": offset,
        "limit": max_chars,
        **({"section": section} if section else {}),
        **({"revision": revision} if revision else {}),
        **({"section_schema": section_schema} if section_schema else {}),
    }
    data = _get(f"/documents/{doc_type}/{article_id}/read?" + urlencode(params))
    if isinstance(data, ToolResult):
        if doc_type == "note" and (data.meta or {}).get("status") == 409 and revision:
            from rag.search import invalidate_document

            invalidate_document(article_id, revision)
        return data
    try:
        return _shape(
            project(
                data, doc_type, article_id, section, offset, max_chars, revision, section_schema
            )
        )
    except (ValueError, TypeError, KeyError):
        return unavailable(
            "Document range or provenance could not be verified; no body was recorded."
        )


@tool
def get_article_stats(article_id: Annotated[StrictInt, Field(gt=0, le=2_147_483_647)]) -> object:
    """Read public article view/like/favorite/comment counters without reading its body.
    This is a read-only snapshot and does not add a view. Unavailable counters remain unknown.
    """
    data = _get(f"/notes/{article_id}/stats")
    if isinstance(data, ToolResult):
        return data
    if not isinstance(data, dict):
        return unavailable("Article counters could not be verified.")
    counts = {
        key: data[key]
        for key in ("views", "likes", "favorites", "comments")
        if type(data.get(key)) is int and data[key] >= 0
    }
    if not counts:
        return unavailable("Article counters could not be verified.")
    return _shape({"noteKey": article_id, **counts})


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
            if hit.get("section_schema"):
                hint += f" section_schema={hit['section_schema']}"
            offset = match.get("offset")
            if type(offset) is int and offset >= 0:
                hint += f" offset={offset}"
        hints.append(hint)
    return text + (" " + " | ".join(hints) if hints else "")


def _rag_meta(route, corpus, top_k):
    mode = route["mode"]
    incomplete = mode in {"degraded", "unavailable"} or route["missing"] > 0
    semantic = ("incomplete" if incomplete else "completed"
                if mode == "hybrid" or route["reason"] == "no_vector_hits" else "not_used")
    return {"corpus": corpus, "rag_mode": mode, "rag_reason": route["reason"],
            "vector_hits": route["vectors"], "vector_missing": route["missing"],
            "retrieval": {"scope": "public_article_body_index", "mode": mode,
                          "semantic": semantic, "snapshot_stale": corpus["stale"], "limit": top_k}}


def _rag_text(meta, text):
    coverage = json.dumps({"retrieval": meta["retrieval"]},
                          ensure_ascii=False, separators=(",", ":"))
    return coverage + "\n" + text


@tool
def rag_search(
    query: Annotated[str, "检索关键词（用户问题中希望从博客内容里找到答案的核心表述）"],
    top_k: Annotated[StrictInt, Field(ge=1, le=64), "返回候选文档数量，默认 8"] = DEFAULT_TOP_K,
) -> str | ToolResult:
    """按主题检索公开文章候选与命中小节，不返回全文。

    推荐可用候选链接；解读需用get_article_detail按命中section及revision补读。
    公告、留言和说说的关键词查询使用public_activity领域，不在此语料中。
    """
    from agent.runtime.budget import BudgetExceeded
    from agent.runtime.control import AgentCancelled

    try:
        from rag.search import get_index, invalidate_document, last_route, search, warm_async
        from rag.visibility import VisibilityUnavailable, current_public

        hits = search(query, top_k=top_k)
        route = last_route()
        corpus = get_index().status()
        meta = _rag_meta(route, corpus, top_k)
        if hits is None:
            return unavailable(
                _rag_text(meta, "当前公开文章索引不可用，未完成本次检索。"), meta=meta)
        if not hits:
            if meta["retrieval"]["semantic"] == "incomplete" or corpus["stale"]:
                return unavailable(_rag_text(
                    meta, "本次未取得候选，检索尚未完整完成，不能据此判断无相关文章。"), meta=meta)
            return empty(_rag_text(meta, "本次查询在当前公开文章索引中未命中。"), meta=meta)
        try:
            verified = current_public(hits, _get)
        except VisibilityUnavailable as error:
            if error.reason == "missing_source_revision":
                warm_async()
            return unavailable(
                _rag_text(meta, "暂时无法核实检索候选的当前公开版本，请稍后重试。"),
                meta={**meta, "reason": error.reason}
            )
        for changed in verified.changed:
            invalidate_document(changed["id"], changed.get("document_revision", ""))
        if verified.changed:
            corpus = {**corpus, "stale": True}
            meta = _rag_meta(route, corpus, top_k)
        hits = verified.current
        if not hits:
            return unavailable(
                _rag_text(meta, "文章公开范围或版本已变化，索引正在刷新，请稍后重新检索。"),
                meta={**meta, "reason": "corpus_changed"},
            )
        text = "\n".join(_rag_hit(i + 1, hit) for i, hit in enumerate(hits))
        if corpus["stale"]:
            text += (
                "\n注：语料刷新未完成，本次候选使用最近一次完整索引；正文读取仍核对当前公开版本。"
            )
        return ok(
            _rag_text(meta, text),
            data=hits,
            meta={
                **meta,
                "visibility_checked": len(verified.current) + len(verified.changed),
                "visibility_dropped": len(verified.changed),
            },
        )
    except (AgentCancelled, BudgetExceeded, TimeoutError):
        raise
    except Exception as exc:
        logger.error("rag_search failed: %s", exc)
        return unavailable(f"检索服务不可用（{type(exc).__name__}）")


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
