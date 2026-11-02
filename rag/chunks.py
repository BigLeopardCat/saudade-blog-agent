"""Structure-aware, bounded index chunks with source offsets for later reading."""

from __future__ import annotations

import hashlib
import json
from importlib.metadata import version

from langchain_text_splitters import RecursiveCharacterTextSplitter

from rag.sections import outline, parser_signature

INDEX_VERSION = "commonmark-path-langchain-v4"
MAX_CHARS = 1800
OVERLAP = 180
SEPARATORS = ["\n\n", "\n", "。", "；", " ", ""]


def strategy_signature() -> str:
    """Pinned library and source-window policy belong to the vector namespace."""
    policy = {
        "version": INDEX_VERSION,
        "parser": parser_signature(),
        "library": version("langchain-text-splitters"),
        "max_chars": MAX_CHARS,
        "overlap": OVERLAP,
        "separators": SEPARATORS,
    }
    return hashlib.sha256(json.dumps(policy, sort_keys=True).encode()).hexdigest()


def _windows(text: str, budget: int):
    splitter = RecursiveCharacterTextSplitter(
        chunk_size=budget,
        chunk_overlap=min(OVERLAP, budget // 4),
        separators=SEPARATORS,
        keep_separator="end",
        strip_whitespace=False,
        add_start_index=True,
    )
    for document in splitter.create_documents([text]):
        start = document.metadata["start_index"]
        body = document.page_content
        if start < 0 or text[start:start + len(body)] != body:
            raise ValueError("splitter returned an invalid source range")
        yield start, start + len(body), body


def split(title: str, content: str) -> list[dict]:
    chunks = []
    for section in outline(content, title):
        if not section["text"].strip():
            continue
        # Metadata retains full paths; a bounded prefix keeps adversarially long
        # headings from consuming an entire embedding request.
        path = " > ".join(section["path"])[:min(600, MAX_CHARS // 3)]
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
                    "prefix_chars": len(prefix),
                    "start": start,
                    "end": end,
                    "chunk_id": hashlib.sha256(identity.encode()).hexdigest()[:24],
                }
            )
    return chunks
