"""Structure-aware, bounded index chunks with source offsets for later reading."""

from __future__ import annotations

import hashlib

from rag.sections import outline

INDEX_VERSION = "markdown-path-v2"
MAX_CHARS = 1800
OVERLAP = 180


def _windows(text: str, budget: int):
    start = 0
    while start < len(text):
        end = min(start + budget, len(text))
        if end < len(text):
            # Prefer a paragraph/line boundary, retaining enough context on both sides.
            cut = text.rfind("\n", start + budget // 2, end)
            if cut >= 0:
                end = cut + 1
        yield start, end, text[start:end]
        if end == len(text):
            break
        start = max(start + 1, end - min(OVERLAP, budget // 4))


def split(title: str, content: str) -> list[dict]:
    chunks = []
    for section in outline(content, title):
        if not section["text"].strip():
            continue
        # Metadata retains full paths; a bounded prefix keeps adversarially long
        # headings from consuming an entire embedding request.
        path = " > ".join(section["path"])[:600]
        prefix = (path + "\n") if path else ""
        for start, end, body in _windows(section["text"], MAX_CHARS - len(prefix)):
            text = prefix + body
            identity = f"{section['section_id']}:{start}:{end}:{text}"
            chunks.append(
                {
                    "section": section["section"],
                    "section_id": section["section_id"],
                    "section_path": section["path"],
                    "text": text,
                    "start": start,
                    "end": end,
                    "chunk_id": hashlib.sha256(identity.encode()).hexdigest()[:24],
                }
            )
    return chunks
