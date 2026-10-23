"""Document aggregation shared by lexical and vector retrieval."""

from __future__ import annotations


def documents(chunks: list[dict], scores: dict[int, float], revision: str) -> list[dict]:
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
                "document_revision": chunk.get("revision"),
                "sections": [],
                "section_ids": [],
                "matches": [],
            }
            by_doc[identity] = row
        section_id = chunk.get("section_id", chunk["section"])
        match = {
            "chunk_id": chunk.get("chunk_id"),
            "section_id": section_id,
            "offset": chunk.get("start", 0),
            "end": chunk.get("end"),
        }
        if match not in row["matches"]:
            row["matches"].append(match)
        if section_id not in row["section_ids"]:
            row["section_ids"].append(section_id)
            row["sections"].append(chunk["section"])
    for row in by_doc.values():
        row["sections"] = row["sections"][:2]
        row["section_ids"] = row["section_ids"][:2]
        row["matches"] = row["matches"][:3]
    return sorted(by_doc.values(), key=lambda row: -row["score"])
