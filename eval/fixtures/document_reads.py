"""Synthetic host document pages for offline API and evidence tests."""

from __future__ import annotations

import re

from rag import sections
from rag.sections import outline, reading_revision

DEFAULT_READ_CHARS = 12000
MAX_READ_CHARS = 24000


class RevisionChanged(ValueError):
    pass


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip().casefold()


def _section(rows: list[dict], want: str) -> int | None:
    w = _norm(want)
    groups = [
        [i for i, row in enumerate(rows) if row["section_id"] == w],
        [i for i, row in enumerate(rows) if _norm(row["section"]) == w],
    ]
    if re.fullmatch(r"\d+(?:\.\d+)*\.?", w):
        groups.append(
            [
                i
                for i, row in enumerate(rows)
                if (match := re.match(r"^\d+(?:\.\d+)*", row["section"]))
                and match.group() == w.rstrip(".")
            ]
        )
    groups.append([i for i, row in enumerate(rows) if w in _norm(row["section"])])
    for matches in groups:
        if matches:
            return matches[0] if len(matches) == 1 else None
    return None


def _scope(rows: list[dict], i: int) -> list[dict]:
    end = i + 1
    while end < len(rows) and rows[end]["level"] > rows[i]["level"]:
        end += 1
    return rows[i:end]


def _validate_range(offset: int, max_chars: int, section: str, revision: str) -> None:
    if type(offset) is not int or offset < 0:
        raise ValueError("offset must be a non-negative integer")
    if type(max_chars) is not int or not 1 <= max_chars <= MAX_READ_CHARS:
        raise ValueError(f"max_chars must be between 1 and {MAX_READ_CHARS}")
    if (offset > 0 or re.fullmatch(r"s\d+", section)) and not revision:
        raise ValueError("续读或按小节 id 读取时必须带 contentRevision，防止跨版本拼接")


def _directory(rows):
    return [{"id": row["section_id"], "title": row["section"], "path": row["path"]}
            for row in rows if row["level"]]


def _outline_page(article_id, title, content, revision, available, offset):
    if offset > len(available):
        raise ValueError("offset exceeds section directory")
    end = min(len(available), offset + 12)
    return {
        "view": "outline", "noteKey": article_id, "noteTitle": title, "noteContent": "",
        "contentRevision": revision, "sectionSchema": sections.PARSER_VERSION,
        "readOffset": 0, "nextOffset": None, "totalChars": len(content),
        "readComplete": False, "source": None, "sections": available[offset:end],
        "sectionCount": len(available), "sectionsOffset": offset,
        "nextSectionsOffset": end if end < len(available) else None,
    }


def article_page(
    data: dict,
    article_id: int,
    section: str = "",
    offset: int = 0,
    max_chars: int = DEFAULT_READ_CHARS,
    revision: str = "",
    view: str = "body",
) -> dict:
    _validate_range(offset, max_chars, section, revision)
    content = data.get("noteContent") or data.get("content") or ""
    title = data.get("noteTitle") or data.get("title") or ""
    current = reading_revision(title, content)
    if revision and revision != current:
        raise RevisionChanged("读取版本不一致，请从头重新读取以获取当前目录")
    rows = outline(content, title)
    available = _directory(rows)
    if view not in {"body", "outline", "metadata"} or view == "outline" and section:
        raise ValueError("invalid document view")
    if view == "metadata":
        if section or offset:
            raise ValueError("metadata does not select body ranges")
        return {"view": "metadata", "noteKey": article_id, "noteTitle": title,
                "contentRevision": current, "sections": [], "sectionCount": 0,
                "sectionsOffset": 0, "nextSectionsOffset": None,
                "readComplete": False, "source": None}
    if view == "outline":
        return _outline_page(article_id, title, content, current, available, offset)
    selected_id = ""
    out = {**data, "noteKey": article_id, "noteTitle": title}
    if section:
        i = _section(rows, section)
        if i is None:
            return {
                "noteKey": article_id,
                "noteTitle": title,
                "readSection": section,
                "sectionText": "",
                "availableSections": [r["title"] for r in available],
                "view": "body", "sections": available[:12],
                "sectionCount": len(available), "sectionsOffset": 0,
                "nextSectionsOffset": 12 if len(available) > 12 else None,
                "contentRevision": current,
                "note": "小节不存在或指称不唯一，本次未读到正文；请用 sections 中的 id 消歧。",
            }
        row = rows[i]
        selected_id = row["section_id"]
        scope = _scope(rows, i)
        selected = content[row["body_start"] : scope[-1]["body_end"]]
        # Empty parents have readable children; a truly empty section has no body.
        if not any(r["text"].strip() for r in scope):
            selected = ""
        out = {
            "noteKey": article_id,
            "noteTitle": title,
            "readSection": row["section"],
            "subsections": [r["section"] for r in scope[1:]],
        }
        body_key = "sectionText"
    else:
        selected, body_key = content, "noteContent"
        out.pop("content", None)
    if offset > len(selected):
        raise ValueError(f"offset exceeds selected content length ({len(selected)})")
    end = min(len(selected), offset + max_chars)
    out[body_key] = selected[offset:end]
    out.update(
        contentRevision=current,
        sectionSchema=sections.PARSER_VERSION,
        readOffset=offset,
        nextOffset=(end if end < len(selected) else None),
        totalChars=len(selected),
        readComplete=end == len(selected),
        view="body", sections=available[:12], sectionCount=len(available),
        sectionsOffset=0, nextSectionsOffset=12 if len(available) > 12 else None,
    )
    out["source"] = {
        "type": "note",
        "id": article_id,
        "revision": current,
        "section_schema": sections.PARSER_VERSION,
        "section_id": selected_id,
        "offset": offset,
        "end": end,
    }
    if end < len(selected):
        out["note"] = (
            f"本次为节选；后续正文用同一文章/section、offset={end}、"
            f"revision={current} 继续读取，或按 sections 的小节 id 读取。"
        )
    elif section:
        if not selected:
            out["note"] = "这一节没有正文内容，请选择 sections 中其他小节。"
            out["availableSections"] = [
                r["section"]
                for j, r in enumerate(rows)
                if r["level"] and any(c["text"].strip() for c in _scope(rows, j))
            ]
        elif out["subsections"]:
            out["note"] = (
                f"本节为节选读取（只含《{out['readSection']}》及其子节 "
                + " / ".join("§" + s for s in out["subsections"])
                + "，不含文章其他部分）。"
            )
        else:
            out["note"] = (
                f"本节为节选读取（只含《{out['readSection']}》这一小节，不含文章其他部分）。"
            )
    return out


def document_page(kind, identifier, title, content, params=None, metadata=None):
    """Emit the public HTTP read protocol from a finite synthetic document."""
    params = params or {}
    section = params.get("section", "")
    if params.get("view") == "metadata" and "limit" in params:
        raise ValueError("metadata does not select body ranges")
    value = article_page(
        {"noteTitle": title, "noteContent": content},
        identifier,
        section,
        int(params.get("offset", 0)),
        int(params.get("limit", 12000)),
        params.get("revision", ""),
        params.get("view", "body"),
    )
    body = value.get("sectionText" if section else "noteContent", "")
    source = value.get("source")
    if source:
        source = {**source, "type": kind}
    if value["view"] == "metadata":
        return {"view": "metadata", "type": kind, "id": identifier, "title": title,
                "metadata": metadata or {}, "contentRevision": value["contentRevision"],
                "sectionSchema": sections.PARSER_VERSION, "readComplete": False, "source": None}
    return {
        "view": value["view"], "type": kind,
        "id": identifier,
        "title": title,
        "content": body,
        "contentRevision": value["contentRevision"],
        "sectionSchema": sections.PARSER_VERSION,
        "readOffset": value.get("readOffset", 0),
        "nextOffset": value.get("nextOffset"),
        "totalChars": value.get("totalChars", 0),
        "readComplete": value.get("readComplete", False),
        "sections": value.get("sections", []),
        "sectionCount": value.get("sectionCount", len(value.get("sections", []))),
        "sectionsOffset": value["sectionsOffset"],
        "nextSectionsOffset": value["nextSectionsOffset"],
        "readSection": value.get("readSection"),
        "source": source,
        "metadata": metadata or {},
    }
