"""Budget validated article frames without losing visible read cursors."""


def render_article_result(data, text: str, cap: int) -> tuple[str, dict | None]:
    """Budget an article frame while keeping a parseable, truthful read cursor."""
    from rag.sections import frame_excerpt, slim_frame

    text = slim_frame(data if isinstance(data, dict) else text)
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
        "id",
        "title",
        "noteId",
        "noteKey",
        "noteTitle",
        "readSection",
        "contentRevision",
        "sectionSchema",
        "readOffset",
        "nextOffset",
        "totalChars",
        "readComplete",
        "source",
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
        if len(str(candidate(mid))) <= cap:
            low = mid
        else:
            high = mid - 1
    if not low:
        return "本轮剩余上下文不足以读取正文，请缩小范围后再试。"[:cap], None
    visible = candidate(low)
    return str(visible), visible
