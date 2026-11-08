"""Document aggregation shared by lexical and vector retrieval."""

from __future__ import annotations

import math
import re

from rag.sections import PARSER_VERSION
from rag.tokenization import tokenize

EXCERPT_CHARS = 240
MATCH_FIELDS = ("title", "heading", "body")


def source_excerpt(match: dict) -> dict | None:
    """Accept only bounded source text with coordinates inside its own locator."""
    excerpt = match.get("excerpt")
    if not isinstance(excerpt, dict) or not isinstance(excerpt.get("text"), str):
        return None
    low, high, start, end = (match.get("offset"), match.get("end"),
                             excerpt.get("offset"), excerpt.get("end"))
    if (type(low) is not int or type(high) is not int
            or type(start) is not int or type(end) is not int):
        return None
    if (not 0 <= low <= start < end <= high
            or end - start != len(excerpt["text"]) or end - start > EXCERPT_CHARS):
        return None
    return excerpt


def _annotate(chunk: dict, match: dict, score: float, retriever: str, query_terms) -> None:
    text, prefix = chunk.get("text"), chunk.get("prefix_chars", 0)
    if (not isinstance(text, str) or type(prefix) is not int or not 0 <= prefix <= len(text)
            or type(match.get("offset")) is not int or type(match.get("end")) is not int
            or match["offset"] < 0 or match["end"] - match["offset"] != len(text) - prefix):
        return
    body = text[prefix:]
    terms = set(query_terms)
    fields = {"title": str(chunk.get("title", ""))[:400], "heading": text[:prefix], "body": body}
    matched = {field: terms.intersection(tokenize(value)) for field, value in fields.items()}
    match["matched_fields"] = [field for field in MATCH_FIELDS if matched[field]]
    match["retrieval_scores"] = {retriever: float(score)}
    positions = []
    for term in matched["body"]:
        pattern = re.escape(term)
        if term.isascii():
            pattern = r"(?<![a-zA-Z0-9_.])" + pattern + r"(?![a-zA-Z0-9_.])"
        found = re.search(pattern, body, re.IGNORECASE | re.ASCII)
        if found:
            positions.append(found.start())
    start = max(0, min(positions, default=0) - EXCERPT_CHARS // 3)
    snippet = body[start:start + EXCERPT_CHARS]
    if snippet:
        match["excerpt"] = {"text": snippet, "offset": match["offset"] + start,
                            "end": match["offset"] + start + len(snippet)}


def merge_match(target: dict, incoming: dict) -> None:
    """Combine evidence for one source location without rescaling retrieval scores."""
    left, right = source_excerpt(target), source_excerpt(incoming)
    if (left and right and (left["offset"], left["end"]) == (right["offset"], right["end"])
            and left["text"] != right["text"]):
        raise ValueError("Cannot fuse conflicting source excerpts")
    if right and not left:
        target["excerpt"] = dict(right)
    scores = {name: value for row in (target, incoming)
              for name, value in (row.get("retrieval_scores") or {}).items()
              if name in {"bm25", "vector"}
              and type(value) in (int, float) and math.isfinite(value)}
    if scores:
        target["retrieval_scores"] = scores
    fields = [name for name in MATCH_FIELDS if any(
        name in (row.get("matched_fields") or []) for row in (target, incoming))]
    if fields:
        target["matched_fields"] = fields


def documents(chunks: list[dict], scores: dict[int, float], revision: str, *,
              retriever: str = "bm25", query_terms=(), limit: int | None = None) -> list[dict]:
    by_doc: dict[tuple[str, int], dict] = {}
    for i, score in sorted(scores.items(), key=lambda item: -item[1]):
        chunk = chunks[i]
        identity = (chunk["type"], chunk["id"])
        row = by_doc.get(identity)
        if row is None:
            row = {
                "type": chunk["type"],
                "id": chunk["id"],
                "title": chunk["title"],
                "score": score,
                "corpus_revision": revision,
                "content_revision": chunk.get("revision"),
                "section_schema": PARSER_VERSION,
                "document_revision": chunk.get("reading_revision", chunk.get("revision")),
                "sections": [],
                "section_ids": [],
                "matches": [],
            }
            by_doc[identity] = row
        section_id = chunk.get("section_id", chunk["section"])
        if section_id in row["section_ids"] or len(row["section_ids"]) >= 3:
            continue
        row["section_ids"].append(section_id)
        row["sections"].append(chunk["section"])
        row["matches"].append({
            "chunk_id": chunk.get("chunk_id"),
            "section_id": section_id,
            "offset": chunk.get("start", 0),
            "end": chunk.get("end"),
            "_source_index": i,
        })
    ranked = sorted(by_doc.values(), key=lambda row: -row["score"])
    if limit is not None:
        ranked = ranked[:max(1, limit)]
    for row in ranked:
        for match in row["matches"]:
            index = match.pop("_source_index")
            _annotate(chunks[index], match, scores[index], retriever, query_terms)
    return ranked
