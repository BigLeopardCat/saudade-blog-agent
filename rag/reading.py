"""Budget validated article frames without losing visible read cursors."""

import json

from rag.context_windows import merge_read_windows
from rag.ranking import source_excerpt
from rag.reading_requests import tool_args
from rag.sections import PARSER_VERSION
from rag.source import BodyPassage, DocumentReference, SourceRange


def article_frame_text(data):
    """Serialize the same article representation for budgeting and model delivery."""
    body = data.get("noteContent")
    if isinstance(body, str) and body:
        data = {key: value for key, value in data.items() if key != "content" or value != body}
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def read_requests(hit):
    """The same complete read requests are used for presentation and guard binding."""
    reads = []
    try:
        reference = DocumentReference(
            type=hit["type"],
            id=hit["id"],
            revision=hit["document_revision"],
            section_schema=hit["section_schema"],
        )
    except (KeyError, TypeError, ValueError):
        return reads
    if reference.section_schema != PARSER_VERSION:
        return reads
    matches = hit.get("matches") or []
    for window in merge_read_windows(matches):
        section = window["section_id"]
        match = matches[window["priority"]]
        source = SourceRange(
            type=hit["type"],
            id=hit["id"],
            revision=hit["document_revision"],
            section_schema=hit["section_schema"],
            section_id=section,
            offset=window["offset"],
            end=window["end"],
        )
        if source.section_schema != PARSER_VERSION or source.end == source.offset:
            raise ValueError("unsupported retrieval source coordinates")
        args = {
            "article_id": hit["id"],
            "doc_type": hit["type"],
            "view": "source_range",
            "section": section,
            "offset": window["offset"],
            "max_chars": min(24000, window["end"] - window["offset"]),
            "revision": hit.get("document_revision"),
            "section_schema": hit.get("section_schema"),
        }
        if args["revision"] and args["section_schema"]:
            reads.append({"path": match.get("section_path", []), "request": args})
    return reads


def candidate_view(index, hit):
    """Model-facing JSON uses executable read coordinates and verified body passages."""
    reads = read_requests(hit)
    passages = []
    for value in hit.get("passages", []):
        passage = BodyPassage.model_validate(value)
        if (passage.source.type, passage.source.id, passage.source.revision,
                passage.source.section_schema) != (
            hit["type"],
            hit["id"],
            hit.get("document_revision"),
            hit.get("section_schema"),
        ):
            raise ValueError("passage does not belong to the candidate")
        passages.append(
            {
                "range": passage.source.model_dump(include={"section_id", "offset", "end"}),
                "text": passage.text,
            }
        )
    candidate = {
        "rank": index,
        "type": hit["type"],
        "id": hit["id"],
        "title": hit["title"][:255],
        "titleTruncated": len(hit["title"]) > 255,
        "passages": passages,
        "read_requests": [
            {
                **read,
                "request": tool_args({key: value for key, value in read["request"].items()
                                      if key not in {"revision", "section_schema"}}),
            }
            for read in reads
        ],
    }
    if reads or passages:
        candidate["source"] = DocumentReference(
            type=hit["type"],
            id=hit["id"],
            revision=hit["document_revision"],
            section_schema=hit["section_schema"],
        ).model_dump()
    if not passages:
        candidate["index_matches"] = [
            {
                **{
                    key: match[key]
                    for key in ("section_id", "section_path", "matched_fields", "retrieval_scores")
                    if key in match
                },
                **({"index_excerpt": excerpt} if (excerpt := source_excerpt(match)) else {}),
            }
            for match in hit.get("matches", [])
        ]
    return candidate


def candidate_text(index, hit):
    return json.dumps(candidate_view(index, hit), ensure_ascii=False, separators=(",", ":"))


def _search_fields(meta):
    return {
        key: meta[key]
        for key in ("retrieval", "presentation", "context", "body_evidence")
        if key in meta
    }


def search_frame(data, meta):
    return json.dumps(
        {
            **_search_fields(meta),
            "documents": [candidate_view(i + 1, hit) for i, hit in enumerate(data)],
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )


def search_text(meta, text):
    return json.dumps(
        {**_search_fields(meta), "message": text}, ensure_ascii=False, separators=(",", ":")
    )


def render_search_result(data, meta, cap):
    """Retain complete ranked sources; unshown passages never establish read evidence."""
    from tools.result import ok, unavailable

    def present(rows):
        visible_meta = dict(meta)
        if len(rows) < len(data):
            visible_meta["presentation"] = {
                "shown_candidates": len(rows),
                "omitted_candidates": len(data) - len(rows),
            }
        if "body_evidence" in meta:
            visible_meta["body_evidence"] = {
                **meta["body_evidence"],
                "returned_chars": sum(
                    len(p["text"]) for row in rows for p in row.get("passages", [])
                ),
            }
        body = search_frame(rows, visible_meta)
        return ok(body, meta=visible_meta, data=rows) if len(body) <= cap else None

    try:
        for size in range(len(data), 0, -1):
            if result := present(data[:size]):
                return result
        if data:
            first = {**data[0], "passages": list(data[0].get("passages", []))}
            while first["passages"]:
                first["passages"].pop()
                if result := present([first]):
                    return result
    except (KeyError, TypeError, ValueError):
        return unavailable(
            "Invalid retrieval source representation."[:cap],
            meta={**meta, "reason": "invalid_retrieval_data"},
        )
    return unavailable(
        "本轮剩余上下文不足以展示完整检索定位，请缩小范围后重试。"[:cap],
        meta={
            **meta,
            "reason": "presentation_budget",
            "presentation": {"shown_candidates": 0, "omitted_candidates": len(data)},
        },
    )


def _outline_frame(data, cap):
    """Shorten the directory page, never an individual locator or its revision."""
    fields = ("view", "noteId", "noteTitle", "id", "title", "contentRevision",
              "sectionSchema", "readOffset", "nextOffset", "totalChars", "readComplete",
              "sectionCount", "sectionsOffset", "source")
    base = {key: data[key] for key in fields if key in data}
    for key in ("noteTitle", "title"):
        if key in base:
            base[key] = base[key][:160]
    rows = data.get("sections") or []
    start, total = data["sectionsOffset"], data["sectionCount"]
    for size in range(len(rows), -1, -1):
        end = start + size
        frame = {**base, "sections": rows[:size],
                 "nextSectionsOffset": end if end < total else None}
        if len(article_frame_text(frame)) <= cap and (size or start == total):
            return article_frame_text(frame), frame
    return "本轮剩余上下文不足以读取目录，请缩小范围后再试。"[:cap], None


def render_article_result(data, text: str, cap: int) -> tuple[str, dict | None]:
    """Budget an article frame while keeping a parseable, truthful read cursor."""
    from rag.sections import frame_excerpt, slim_frame

    text = article_frame_text(data) if isinstance(data, dict) else slim_frame(text)
    if len(text) <= cap:
        return text, data if isinstance(data, dict) else None
    if not isinstance(data, dict):
        return frame_excerpt(text, cap)[:cap], None
    if data.get("view") == "metadata":
        return "本轮剩余上下文不足以完整展示元数据。"[:cap], None
    if data.get("view") == "outline":
        return _outline_frame(data, cap)
    key = next(
        (
            k
            for k in ("sectionText", "noteContent", "content")
            if isinstance(data.get(k), str) and data[k]
        ),
        None,
    )
    if key is None:
        return text[:cap], None
    fields = (
        "id",
        "title",
        "noteId",
        "noteKey",
        "noteTitle",
        "readSection",
        "selectedScope",
        "view",
        "contentRevision",
        "sectionSchema",
        "readOffset",
        "nextOffset",
        "totalChars",
        "readComplete",
        "source",
        "omittedViews",
        "authorName",
        "authorNameTruncated",
        "authorAvatar",
        "authorAvatarTruncated",
        "description",
        "descriptionTruncated",
        "cover",
        "coverTruncated",
        "coverFit",
        "coverFocusX",
        "coverFocusY",
        "coverZoom",
        "carouselFit",
        "carouselFocusX",
        "carouselFocusY",
        "carouselZoom",
    )
    out = {k: data[k] for k in fields if k in data}
    title_key = "noteTitle" if "noteTitle" in out else "title"
    out[title_key] = str(out.get(title_key) or "")[:160]
    body = data[key]
    offset = int(data.get("readOffset") or 0)

    def candidate(n: int) -> dict:
        row = {**out, key: body[:n], "note": "正文按本轮剩余预算节选，可用 nextOffset 继续读取。"}
        if n < len(body):
            row.update(nextOffset=offset + n, readComplete=False)
            if isinstance(row.get("source"), dict):
                row["source"] = {**row["source"], "end": offset + n}
        return row

    low, high = 0, len(body)
    while low < high:
        mid = (low + high + 1) // 2
        if len(article_frame_text(candidate(mid))) <= cap:
            low = mid
        else:
            high = mid - 1
    if not low:
        return "本轮剩余上下文不足以读取正文，请缩小范围后再试。"[:cap], None
    visible = candidate(low)
    return article_frame_text(visible), visible
