"""Article read ranges and revision-bound, unambiguous section addressing."""

from __future__ import annotations

import re

from rag.corpus import content_revision
from rag.sections import outline, render

DEFAULT_READ_CHARS = 6000
MAX_READ_CHARS = 12000


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


def article_page(
    data: dict,
    article_id: int,
    section: str = "",
    offset: int = 0,
    max_chars: int = DEFAULT_READ_CHARS,
    revision: str = "",
) -> dict:
    _validate_range(offset, max_chars, section, revision)
    content = data.get("noteContent") or data.get("content") or ""
    title = data.get("noteTitle") or data.get("title") or ""
    current = content_revision(title, content)
    if revision and revision != current:
        raise RevisionChanged("文章已更新，请重新检索后读取当前版本")
    rows = outline(content, title)
    available = [
        {"id": r["section_id"], "title": r["section"], "path": r["path"]}
        for r in rows
        if r["level"]
    ]
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
                "sections": available,
                "contentRevision": current,
                "note": "小节不存在或指称不唯一，本次未读到正文；请用 sections 中的 id 消歧。",
            }
        row = rows[i]
        selected_id = row["section_id"]
        scope = _scope(rows, i)
        selected = scope[0]["text"]
        if len(scope) > 1:
            selected += "\n\n" + "\n\n".join(render(r) for r in scope[1:])
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
        readOffset=offset,
        nextOffset=(end if end < len(selected) else None),
        totalChars=len(selected),
        readComplete=end == len(selected),
        sections=available[:12],
        sectionCount=len(available),
    )
    out["source"] = {
        "type": "note",
        "id": article_id,
        "revision": current,
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


def render_article_result(value, cap: int) -> tuple[str, dict | None]:
    """Budget an article frame while keeping a parseable, truthful read cursor."""
    import ast

    from rag.sections import frame_excerpt, slim_frame

    data = getattr(value, "data", None)
    text = slim_frame(str(value))
    if not isinstance(data, dict):
        try:
            data = ast.literal_eval(text)
        except (ValueError, SyntaxError):
            data = None
    if len(text) <= cap:
        return text, data if isinstance(data, dict) else None
    if not isinstance(data, dict):
        return frame_excerpt(text, cap)[:cap], None
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
        "noteId",
        "noteKey",
        "noteTitle",
        "readSection",
        "contentRevision",
        "readOffset",
        "nextOffset",
        "totalChars",
        "readComplete",
        "source",
    )
    out = {k: data[k] for k in fields if k in data}
    title = str(out.get("noteTitle") or "")
    out["noteTitle"] = title[:160]
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
        if len(str(candidate(mid))) <= cap:
            low = mid
        else:
            high = mid - 1
    if not low:
        return "本轮剩余上下文不足以读取正文，请缩小范围后再试。"[:cap], None
    visible = candidate(low)
    return str(visible), visible
