"""Validate source spans and score returned locations, independently of answer generation."""

from __future__ import annotations

from rag.corpus import content_revision
from rag.sections import PARSER_VERSION, outline


def validate(query: dict, corpus: dict, positive: set[str]) -> None:
    scope = query.get("scope_document_id")
    if scope is not None and (scope not in corpus or positive - {scope}):
        raise ValueError("query scope must contain every positive document")
    groups = query.get("evidence_spans", [])
    if not isinstance(groups, list):
        raise ValueError("evidence_spans must be a list of alternative-span groups")
    for group in groups:
        if not isinstance(group, list) or not group:
            raise ValueError("empty evidence span group")
        for span in group:
            _validate_span(span, corpus, positive)


def _validate_span(span: dict, corpus: dict, positive: set[str]) -> None:
    if not isinstance(span, dict) or span.get("document_id") not in positive:
        raise ValueError("evidence span must reference a positive document")
    doc = corpus[span["document_id"]]
    revision = content_revision(doc.get("title", ""), doc["text"])
    if span.get("revision") != revision or span.get("section_schema") != PARSER_VERSION:
        raise ValueError("evidence span source revision/parser mismatch")
    section = next((row for row in outline(doc["text"], doc.get("title", ""))
                    if row["section_id"] == span.get("section_id")), None)
    start, end, quote = span.get("start"), span.get("end"), span.get("quote")
    if (section is None or type(start) is not int or type(end) is not int
            or not 0 <= start < end <= len(section["text"])
            or not isinstance(quote, str) or not quote.strip()
            or section["text"][start:end] != quote):
        raise ValueError("evidence span does not match the exact source range")


def locations(hits: list[dict], keys: list[str]) -> list[dict]:
    """Preserve article ranks and section-relative ranges from the production retriever."""
    return [{"document_id": keys[hit["id"] - 1], "matches": [
        {key: match[key] for key in ("section_id", "offset", "end") if key in match}
        for match in hit.get("matches", [])]} for hit in hits]


def _covered(span: dict, hits: list[dict]) -> bool:
    intervals = sorted((match["offset"], match["end"])
                       for hit in hits if hit["document_id"] == span["document_id"]
                       for match in hit["matches"]
                       if match.get("section_id") == span["section_id"]
                       and type(match.get("offset")) is int
                       and type(match.get("end")) is int)
    cursor = span["start"]
    for start, end in intervals:
        if start > cursor:
            break
        cursor = max(cursor, end)
        if cursor >= span["end"]:
            return True
    return False


def score(groups: list[list[dict]], hits: list[dict], cutoff: int) -> dict:
    selected = hits[:cutoff]
    covered = [any(_covered(span, selected) for span in group) for group in groups]
    return {f"PassageEvidenceCoverage@{cutoff}": sum(covered) / len(groups),
            f"AllPassageEvidence@{cutoff}": float(all(covered))}
