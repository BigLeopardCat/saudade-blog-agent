"""Validate source spans and score returned locations, independently of answer generation."""

from __future__ import annotations

from rag.corpus import content_revision
from rag.sections import PARSER_VERSION, outline


def frozen_labels(data: dict) -> dict:
    """Keep scoring/group metadata and source coordinates without question text or quotes."""
    labels = {"qrels": data["qrels"], "queries": {}}
    for qid, query in data["queries"].items():
        item = {key: query[key] for key in ("category", "evidence_groups", "answerable")
                if key in query}
        if query.get("evidence_spans"):
            item["evidence_spans"] = [{key: span[key] for key in (
                "document_id", "section_id", "start", "end", "group", "revision", "section_schema")
                if key in span} for span in query["evidence_spans"]]
        labels["queries"][qid] = item
    return labels


def validate(query: dict, corpus: dict, positive: set[str]) -> None:
    scope = query.get("scope_document_id")
    if scope is not None and (scope not in corpus or positive - {scope}):
        raise ValueError("query scope must contain every positive document")
    spans = query.get("evidence_spans", [])
    if not isinstance(spans, list):
        raise ValueError("evidence_spans must be a flat list of source spans")
    for span in spans:
        _validate_span(span, corpus, positive)


def _validate_span(span: dict, corpus: dict, positive: set[str]) -> None:
    if not isinstance(span, dict) or span.get("document_id") not in positive:
        raise ValueError("evidence span must reference a positive document")
    doc = corpus[span["document_id"]]
    revision = content_revision(doc.get("title", ""), doc["text"])
    if (span.get("revision", revision) != revision
            or span.get("section_schema", PARSER_VERSION) != PARSER_VERSION):
        raise ValueError("evidence span source revision/parser mismatch")
    section = next((row for row in outline(doc["text"], doc.get("title", ""))
                    if row["section_id"] == span.get("section_id")), None)
    start, end, quote = span.get("start"), span.get("end"), span.get("quote")
    # Original fixtures specify a unique quote. Bind that existing label to the
    # frozen source in memory; explicit coordinates are never silently replaced.
    if ("start" not in span and "end" not in span and section is not None
            and isinstance(quote, str) and quote and section["text"].count(quote) == 1):
        start = section["text"].index(quote)
        end = start + len(quote)
    if (section is None or type(start) is not int or type(end) is not int
            or not 0 <= start < end <= len(section["text"])
            or not isinstance(quote, str) or not quote.strip()
            or section["text"][start:end] != quote):
        raise ValueError("evidence span does not match the exact source range")
    if "group" in span and (not isinstance(span["group"], str) or not span["group"]):
        raise ValueError("alternative span group must be a nonempty string")
    span.update(start=start, end=end, revision=revision, section_schema=PARSER_VERSION)


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


def score(spans: list[dict], hits: list[dict], cutoff: int) -> dict:
    selected = hits[:cutoff]
    groups: dict[tuple, list[dict]] = {}
    for index, span in enumerate(spans):
        key = ("alternative", span["group"]) if "group" in span else ("required", index)
        groups.setdefault(key, []).append(span)
    covered = [any(_covered(span, selected) for span in group) for group in groups.values()]
    return {f"PassageEvidenceCoverage@{cutoff}": sum(covered) / len(groups),
            f"AllPassageEvidence@{cutoff}": float(all(covered))}
