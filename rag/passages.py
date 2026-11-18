"""Extract bounded, exact passages from a version-matched public corpus snapshot."""

from rag.context_windows import merge_read_windows
from rag.ranking import source_excerpt
from rag.sections import PARSER_VERSION, outline, reading_revision
from rag.source import BodyPassage, SourceRange


def _trim(passage, matches, size):
    text, source = passage["text"], passage["source"]
    if size >= len(text):
        return passage
    center = source["offset"]
    for match in matches:
        excerpt = source_excerpt(match)
        if (match["section_id"] == source["section_id"] and excerpt
                and source["offset"] <= excerpt["offset"] < excerpt["end"] <= source["end"]):
            center = (excerpt["offset"] + excerpt["end"]) // 2
            break
    start = max(0, min(len(text) - size, center - source["offset"] - size // 2))
    return {**passage, "text": text[start:start + size], "source": {
        **source, "offset": source["offset"] + start, "end": source["offset"] + start + size}}


def _allowances(sizes, capacity):
    allocated, remaining = [0] * len(sizes), max(0, capacity)
    pending = [i for i, size in enumerate(sizes) if size]
    while pending and remaining:
        share = max(1, remaining // len(pending))
        for index in pending:
            take = min(sizes[index] - allocated[index], share, remaining)
            allocated[index] += take
            remaining -= take
        pending = [i for i in pending if allocated[i] < sizes[i]]
    return allocated


def budget_passages(rows, max_chars):
    """Share a presentation allowance across sources and their distinct evidence ranges."""
    allowances = _allowances([sum(len(p["text"]) for p in row.get("passages", []))
                              for row in rows], max_chars)
    result = []
    for row, allowance in zip(rows, allowances):
        values = row.get("passages", [])
        sizes = _allowances([len(p["text"]) for p in values], allowance)
        passages = [_trim(passage, row.get("matches", []), size)
                    for passage, size in zip(values, sizes) if size]
        result.append({**row, "passages": passages})
    return result


def narrow_locations(rows, width):
    """Keep ranked locations and only the exact body ranges belonging to those locations."""
    narrowed = []
    for row in rows:
        if not row.get("passages"):
            narrowed.append(row)
            continue
        matches = row.get("matches", [])[:width]
        passages = []
        for passage in row.get("passages", []):
            source = passage["source"]
            for window in merge_read_windows(matches):
                if source["section_id"] != window["section_id"]:
                    continue
                low = max(source["offset"], window["offset"])
                high = min(source["end"], window["end"])
                if low < high:
                    passages.append({**passage,
                        "text": passage["text"][low-source["offset"]:high-source["offset"]],
                        "source": {**source, "offset": low, "end": high}})
        sections = [s for s in row.get("section_ids", [])
                    if any(m["section_id"] == s for m in matches)]
        names = dict(zip(row.get("section_ids", []), row.get("sections", [])))
        narrowed.append({**row, "matches": matches, "passages": passages,
                         "section_ids": sections,
                         "sections": [names[s] for s in sections if s in names]})
    return narrowed


def useful_passages(original, presented):
    """A tiny fragment cannot displace a readable query excerpt in another location."""
    for before, after in zip(original, presented):
        if len(before.get("passages", [])) != len(after.get("passages", [])):
            return False
        for source, visible in zip(before.get("passages", []), after.get("passages", [])):
            excerpts = [source_excerpt(m) for m in before.get("matches", [])
                        if m["section_id"] == source["source"]["section_id"]]
            minimum = max((len(e["text"]) for e in excerpts if e), default=128)
            if len(visible["text"]) < min(minimum, len(source["text"])):
                return False
    return True


def attach_passages(hits, sources, corpus_revision, *, max_chars=None):
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
                section_chars=len(section["text"]),
            )
            passages.append(passage.model_dump())
        result.append({**row, "passages": passages})
    return result if max_chars is None else budget_passages(result, max_chars)
