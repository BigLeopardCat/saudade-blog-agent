"""Link provenance and visible body ranges; not a semantic claim validator."""

from __future__ import annotations

import ast
import re
from urllib.parse import urlsplit

SEARCH_TOOLS = frozenset({"rag_search"})


def payload(value) -> dict | None:
    data = getattr(value, "data", None)
    if isinstance(data, dict):
        return data
    try:
        parsed = ast.literal_eval(str(value))
    except (ValueError, SyntaxError):
        return None
    return parsed if isinstance(parsed, dict) else None


def body_text(data: dict | None) -> str:
    if not data:
        return ""
    return str(data.get("sectionText") or data.get("noteContent") or data.get("content") or "")


def _read_range(data, article_id):
    source = data.get("source") if isinstance(data.get("source"), dict) else {}
    start, end = source.get("offset"), source.get("end")
    body = body_text(data)
    located = (source.get("type") == "note" and type(source.get("id")) is int
               and source["id"] == article_id
               and type(start) is int and type(end) is int and 0 <= start <= end
               and end - start == len(body))
    total = data.get("totalChars")
    total_known = located and type(total) is int and total >= end
    return {"revision": source.get("revision") if located else None,
            "section_id": source.get("section_id") if located else None,
            "start": start if located else None, "end": end if located else None,
            "visible_chars": len(body),
            "selected_total_chars": total if total_known else None,
            "selected_range_reaches_end": end == total if total_known else None}


def _article_links(text):
    urls = [*re.findall(r"\]\((/article/\d+[^)]*)\)", text),
            *re.findall(r"https?://[^\s)<>]+", text)]
    for url in urls:
        try:
            parsed = urlsplit(url.rstrip(".,，。"))
        except ValueError:
            continue
        match = re.fullmatch(r"/article/(\d+)/?", parsed.path)
        if match:
            yield parsed.netloc.casefold(), int(match[1])


class ArticleEvidence:
    def __init__(self) -> None:
        self.searched = False
        self.found = False
        self.empty = False
        self.failed = False
        self.documents: dict[int, str] = {}
        self.read_ranges: dict[int, list[dict]] = {}
        self.candidates: dict[int, str] = {}
        self.references: set[tuple[str, int]] = set()

    def snapshot(self):
        return {**vars(self), "references": sorted(self.references), "read_ranges": {
            ident: [dict(row) for row in rows] for ident, rows in self.read_ranges.items()}}

    @classmethod
    def restore(cls, state):
        evidence = cls()
        for key in ("searched", "found", "empty", "failed"):
            setattr(evidence, key, bool(state[key]))
        for key in ("documents", "candidates"):
            setattr(evidence, key, {int(ident): title for ident, title in state[key].items()})
        evidence.read_ranges = {int(ident): [dict(row) for row in rows] for ident, rows
                                in state.get("read_ranges", {}).items()}
        evidence.references = {tuple(row) for row in state["references"]}
        return evidence

    def observe(self, name: str, args: dict, value) -> None:
        kind = getattr(value, "kind", "ok")
        if name in SEARCH_TOOLS or name == "search_notes":
            rows = getattr(value, "data", None)
            if isinstance(rows, list) and kind == "ok":
                for row in rows:
                    if not isinstance(row, dict) or row.get("type", "note") != "note":
                        continue
                    ident = row.get("id", row.get("noteId", row.get("noteKey")))
                    if type(ident) is int and ident > 0:
                        self.candidates[ident] = str(row.get("title", row.get("noteTitle", "文章")))
        if name in SEARCH_TOOLS:
            self.searched = True
            self.empty |= kind == "empty"
            self.failed |= kind in ("unavailable", "not_found", "invalid_args")
            self.found |= kind == "ok" and bool(str(value).strip())
        elif (
            name == "get_article_detail" and args.get("doc_type", "note") == "note" and kind == "ok"
        ):
            data = payload(value)
            ident = args.get("article_id")
            if data and body_text(data).strip() and type(ident) is int and ident > 0:
                self.documents[ident] = str(data.get("noteTitle") or "文章")
                ranges = self.read_ranges.setdefault(ident, [])
                observed = _read_range(data, ident)
                if observed not in ranges:
                    ranges.append(observed)
                self.references.update(_article_links(body_text(data)))

    def provenance(self, site):
        """Distinguish known links from read content without claiming answer support."""
        origin = urlsplit(site).netloc.casefold()
        return {
            "candidate_articles": [{"id": ident, "title": title}
                                   for ident, title in self.candidates.items()],
            "body_reads": [{"id": ident, "title": title,
                            "ranges": [dict(row) for row in self.read_ranges.get(ident, [])]}
                           for ident, title in self.documents.items()],
            "mentioned_article_ids": sorted({ident for host, ident in self.references
                                              if not host or host == origin}),
        }

    def issue(self, reply: str, site: str) -> str | None:
        """Validate references only; content grounding requires semantic evaluation."""
        if self.found and not self.documents and not self.candidates:
            return "article_body_missing"
        if not self.searched and not self.documents and not self.candidates:
            return None
        if self.invalid_references(reply, site):
            return "unread_article_reference"
        return None

    def invalid_references(self, reply: str, site: str) -> list[dict]:
        """Explain rejected references without retaining URLs, credentials or answer text."""
        origin = urlsplit(site).netloc.casefold()
        referenced = {ident for host, ident in self.references if not host or host == origin}
        available = {*self.documents, *self.candidates, *referenced}
        return [
            {"article_id": ident, "same_site": not host or host == origin,
             "known_id": ident in available}
            for host, ident in _article_links(reply)
            if ident not in available or host and host != origin
        ]

    def sources(self, reply: str, site: str) -> str:
        origin = urlsplit(site).netloc.casefold()
        cited = {ident for host, ident in _article_links(reply) if not host or host == origin}
        if not self.documents or set(self.documents).issubset(cited):
            return reply
        refs = []
        for article_id, title in self.documents.items():
            if article_id in cited:
                continue
            label = re.sub(r"[\[\]\r\n]", "", title)[:80]
            refs.append(f"[{label}]({site.rstrip('/')}/article/{article_id})")
        return reply + "\n\n来源：" + "、".join(refs)
