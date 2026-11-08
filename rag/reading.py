"""Budget validated article frames without losing visible read cursors."""

import json


def _json_frame(data):
    body = data.get("noteContent")
    if isinstance(body, str) and body:
        data = {key: value for key, value in data.items() if key != "content" or value != body}
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def _source_hint(hit, label, ident, match):
    label = str(label)[:80].replace("\n", " ").replace("\r", " ")
    hint = f"命中节={label}"
    if not ident or not hit.get("document_revision"):
        return hint
    hint += f" section={ident} revision={hit['document_revision']}"
    if hit.get("section_schema"):
        hint += f" section_schema={hit['section_schema']}"
    offset, end = match.get("offset"), match.get("end")
    if type(offset) is int and offset >= 0:
        hint += f" offset={offset}"
        if type(end) is int and end > offset:
            hint += f" max_chars={min(24000, end - offset)}"
    return hint


def candidate_text(index, hit):
    title = hit["title"][:24].replace("\n", " ").replace("\r", " ")
    text = f"{index}. type={hit['type']} id={hit['id']} rank={index} title={title}"
    identifiers = hit.get("section_ids") or []
    sections = list(zip(hit.get("sections") or [], identifiers))[:3]
    matches = hit.get("matches") or []
    chosen = []
    for label, ident in sections:
        match = next((m for m in matches if m.get("section_id") == ident), {})
        chosen.append((label, ident, match))
    for match in matches:
        label = next((label for label, ident in sections if ident == match.get("section_id")), None)
        row = (label, match.get("section_id"), match)
        if label is not None and row not in chosen and len(chosen) < 3:
            chosen.append(row)
    hints = [_source_hint(hit, *row) for row in chosen]
    return text + (" " + " | ".join(hints) if hints else "")


def search_text(meta, text):
    fields = {"retrieval": meta["retrieval"]}
    if "presentation" in meta:
        fields["presentation"] = meta["presentation"]
    coverage = json.dumps(fields,
                          ensure_ascii=False, separators=(",", ":"))
    return coverage + "\n" + text


def render_search_result(data, text, meta, cap):
    """Keep complete candidate entries and expose only their visible data to the agent."""
    from tools.result import ok, unavailable

    if len(text) <= cap:
        return ok(text, meta=meta, data=data)
    try:
        entries = [candidate_text(i + 1, row) for i, row in enumerate(data)]
    except (KeyError, TypeError):
        return unavailable("Invalid retrieval source representation."[:cap],
                           meta={**meta, "reason": "invalid_retrieval_data"})
    for size in range(len(entries) - 1, 0, -1):
        visible_meta = {**meta, "presentation": {
            "shown_candidates": size, "omitted_candidates": len(entries) - size}}
        body = search_text(visible_meta, "\n".join(entries[:size]))
        if len(body) <= cap:
            return ok(body, meta=visible_meta, data=data[:size])
    return unavailable("本轮剩余上下文不足以展示完整检索定位，请缩小范围后重试。"[:cap],
                       meta={**meta, "reason": "presentation_budget", "presentation": {
                           "shown_candidates": 0, "omitted_candidates": len(data)}})


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
        if len(_json_frame(frame)) <= cap and (size or start == total):
            return _json_frame(frame), frame
    return "本轮剩余上下文不足以读取目录，请缩小范围后再试。"[:cap], None


def render_article_result(data, text: str, cap: int) -> tuple[str, dict | None]:
    """Budget an article frame while keeping a parseable, truthful read cursor."""
    from rag.sections import frame_excerpt, slim_frame

    text = _json_frame(data) if isinstance(data, dict) else slim_frame(text)
    if len(text) <= cap:
        return text, data if isinstance(data, dict) else None
    if not isinstance(data, dict):
        return frame_excerpt(text, cap)[:cap], None
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
        "view",
        "contentRevision",
        "sectionSchema",
        "readOffset",
        "nextOffset",
        "totalChars",
        "readComplete",
        "source",
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
        if len(_json_frame(candidate(mid))) <= cap:
            low = mid
        else:
            high = mid - 1
    if not low:
        return "本轮剩余上下文不足以读取正文，请缩小范围后再试。"[:cap], None
    visible = candidate(low)
    return _json_frame(visible), visible
