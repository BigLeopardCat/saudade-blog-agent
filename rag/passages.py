"""Extract bounded, exact passages from a version-matched public corpus snapshot."""

from rag.context_windows import merge_read_windows
from rag.sections import PARSER_VERSION, outline, reading_revision
from rag.source import BodyPassage, SourceRange


def attach_passages(hits, sources, corpus_revision, *, max_chars=12000):
    remaining = max_chars
    result = []
    for hit in hits:
        row = {key: value for key, value in hit.items() if key != "passages"}
        document = sources.get((hit["type"], hit["id"]))
        if (
            document is None
            or hit.get("corpus_revision") != corpus_revision
            or hit.get("section_schema") != PARSER_VERSION
            or reading_revision(document["title"], document["content"])
            != hit.get("content_revision")
            or hit.get("document_revision") != hit.get("content_revision")
        ):
            raise ValueError("passage source snapshot changed")
        sections = {
            section["section_id"]: section
            for section in outline(document["content"], document["title"])
        }
        passages = []
        for window in merge_read_windows(hit.get("matches", [])):
            section = sections.get(window["section_id"])
            start, end = window["offset"], window["end"]
            if (
                section is None
                or type(start) is not int
                or type(end) is not int
                or not 0 <= start < end <= len(section["text"])
            ):
                raise ValueError("retrieval locator differs from source text")
            if not remaining:
                break
            end = min(end, start + remaining)
            passage = BodyPassage(
                source=SourceRange(
                    type=hit["type"],
                    id=hit["id"],
                    revision=hit["document_revision"],
                    section_schema=hit["section_schema"],
                    section_id=window["section_id"],
                    offset=start,
                    end=end,
                ),
                text=section["text"][start:end],
            )
            passages.append(passage.model_dump())
            remaining -= len(passage.text)
        result.append({**row, "passages": passages})
    return result
