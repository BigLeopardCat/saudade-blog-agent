"""Link provenance and visible body ranges; not a semantic claim validator."""

from __future__ import annotations

import json
import re
from urllib.parse import urlsplit

from langchain_core.messages import ToolMessage

from agent.domains.articles.reading import body_continuation

SEARCH_TOOLS = frozenset({"rag_search"})
CATALOG_TOOLS = frozenset({"list_notes", "get_top_notes", "list_admin_notes"})


def catalog_source_url(row):
    """Validate a host catalog link without promoting it to article body evidence."""
    value = row.get("sourceUrl")
    if value is None:
        return None
    if (type(row.get("id")) is not int or row["id"] <= 0
            or not isinstance(value, str) or len(value) > 2048
            or any(ord(char) <= 32 or ord(char) == 127 for char in value)):
        raise ValueError("Invalid public article source URL")
    parsed = urlsplit(value)
    if (parsed.scheme not in ("http", "https") or not parsed.hostname
            or parsed.username is not None or parsed.password is not None
            or parsed.path != f"/article/{row['id']}" or parsed.query or parsed.fragment):
        raise ValueError("Public article source does not match its identity")
    _ = parsed.port  # Validate the port without changing the supplied URL.
    return value


def payload(value):
    """Read source data, never infer executable facts by parsing displayed prose."""
    return (
        (value.artifact or {}).get("data")
        if isinstance(value, ToolMessage)
        else getattr(value, "data", None)
    )


def body_text(data: dict | None) -> str:
    if not data:
        return ""
    return str(data.get("sectionText") or data.get("noteContent") or data.get("content") or "")


def _read_range(data, article_id):
    source = data.get("source") if isinstance(data.get("source"), dict) else {}
    start, end = source.get("offset"), source.get("end")
    body = body_text(data)
    located = (
        source.get("type") == "note"
        and type(source.get("id")) is int
        and source["id"] == article_id
        and type(start) is int
        and type(end) is int
        and 0 <= start <= end
        and end - start == len(body)
    )
    total = data.get("totalChars")
    total_known = located and type(total) is int and total >= end
    return {
        "revision": source.get("revision") if located else None,
        "section_id": source.get("section_id") if located else None,
        "start": start if located else None,
        "end": end if located else None,
        "visible_chars": len(body),
        "selected_total_chars": total if total_known else None,
        "selected_range_reaches_end": end == total if total_known else None,
    }


def _article_links(text):
    urls = [
        *re.findall(r"\]\((/article/\d+[^)]*)\)", text),
        *re.findall(r"https?://[^\s)<>]+", text),
    ]
    try:
        structured = json.loads(text)
        if isinstance(structured, dict) and isinstance(structured.get("source"), str):
            urls.append(structured["source"])
    except (ValueError, RecursionError):
        pass
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
        self.continuations: dict[int, dict] = {}
        self.candidates: dict[int, str] = {}
        self.references: set[tuple[str, int]] = set()
        self.catalog_references: set[tuple[str, int]] = set()

    def snapshot(self):
        return {
            **vars(self),
            "references": sorted(self.references),
            "catalog_references": sorted(self.catalog_references),
            "read_ranges": {
                ident: [dict(row) for row in rows] for ident, rows in self.read_ranges.items()
            },
            "continuations": {ident: dict(cursor) for ident, cursor in self.continuations.items()},
        }

    @classmethod
    def restore(cls, state):
        evidence = cls()
        for key in ("searched", "found", "empty", "failed"):
            setattr(evidence, key, bool(state[key]))
        for key in ("documents", "candidates"):
            setattr(evidence, key, {int(ident): title for ident, title in state[key].items()})
        evidence.read_ranges = {
            int(ident): [dict(row) for row in rows]
            for ident, rows in state.get("read_ranges", {}).items()
        }
        evidence.continuations = {
            int(ident): dict(cursor) for ident, cursor in state.get("continuations", {}).items()
        }
        evidence.references = {tuple(row) for row in state["references"]}
        evidence.catalog_references = {tuple(row) for row in state.get("catalog_references", [])}
        return evidence

    def observe(self, name: str, args: dict, value) -> None:
        kind = (
            (value.artifact or {}).get("kind", "ok")
            if isinstance(value, ToolMessage)
            else getattr(value, "kind", "ok")
        )
        text = (
            (value.content if isinstance(value.content, str) else "")
            if isinstance(value, ToolMessage)
            else str(value)
        )
        if name in SEARCH_TOOLS or name == "search_notes" or name in CATALOG_TOOLS:
            rows = payload(value)
            if name in CATALOG_TOOLS or name == "search_notes" and isinstance(rows, dict):
                rows = (
                    rows.get("items")
                    if isinstance(rows, dict) and rows.get("resource") == "articles"
                    else None
                )
            if isinstance(rows, list) and kind == "ok":
                for row in rows:
                    if not isinstance(row, dict) or row.get("type", "note") != "note":
                        continue
                    ident = row.get("id", row.get("noteId", row.get("noteKey")))
                    if type(ident) is int and ident > 0:
                        self.candidates[ident] = str(row.get("title", row.get("noteTitle", "文章")))
                        if name in {"list_notes", "get_top_notes"}:
                            self.catalog_references = {
                                pair for pair in self.catalog_references if pair[1] != ident}
                            if url := catalog_source_url(row):
                                self.catalog_references.add(
                                    (urlsplit(url).netloc.casefold(), ident))
        if name in SEARCH_TOOLS:
            self.searched = True
            self.empty |= kind == "empty"
            self.failed |= kind in ("unavailable", "not_found", "invalid_args", "error")
            self.found |= kind == "ok" and bool(text.strip())
        elif (name == "get_article_detail" and args.get("doc_type", "note") == "note"
              and type(args.get("article_id")) is int):
            self.continuations.pop(args["article_id"], None)
            if kind == "ok":
                self._observe_detail(args.get("article_id"), payload(value))

    def continuation_args(self, args):
        """Bind only the observed next body page; explicit conflicting guards stay intact."""
        if type(args.get("article_id")) is not int:
            return args
        cursor = self.continuations.get(args.get("article_id"))
        if (not cursor or args.get("doc_type", "note") != "note"
                or args.get("view", "body") != "body" or args.get("section")
                or type(args.get("offset")) is not int or args["offset"] != cursor["offset"]
                or any(args.get(key) and args[key] != cursor[key]
                       for key in ("revision", "section_schema"))):
            return args
        return {**args, **{key: cursor[key] for key in ("revision", "section_schema")
                          if not args.get(key)}}

    def _observe_detail(self, ident, data):
        if not isinstance(data, dict) or type(ident) is not int or ident <= 0:
            return
        body = body_text(data)
        if not body.strip():
            self._observe_metadata(ident, data)
            return
        self.documents[ident] = str(data.get("noteTitle") or "文章")
        ranges = self.read_ranges.setdefault(ident, [])
        observed = _read_range(data, ident)
        if cursor := body_continuation(data, ident):
            self.continuations[ident] = cursor
        if observed not in ranges:
            ranges.append(observed)
        self.references.update(_article_links(body))

    def _observe_metadata(self, ident: int, data: dict) -> None:
        """Verified metadata permits a source link without recording body evidence."""
        source = data.get("source")
        outline = (data.get("view") in {"outline", "metadata"} and type(data.get("noteId")) is int
                   and data["noteId"] == ident
                   and isinstance(data.get("contentRevision"), str)
                   and re.fullmatch(r"[0-9a-f]{64}", data["contentRevision"]))
        if outline or (isinstance(source, dict) and source.get("type") == "note"
                       and type(source.get("id")) is int and source["id"] == ident):
            self.candidates[ident] = str(data.get("noteTitle") or "文章")

    def provenance(self, site):
        """Distinguish known links from read content without claiming answer support."""
        origin = urlsplit(site).netloc.casefold()
        return {
            "candidate_articles": [
                {"id": ident, "title": title} for ident, title in self.candidates.items()
            ],
            "body_reads": [
                {
                    "id": ident,
                    "title": title,
                    "ranges": [dict(row) for row in self.read_ranges.get(ident, [])],
                }
                for ident, title in self.documents.items()
            ],
            "mentioned_article_ids": sorted(
                {ident for host, ident in self.references if not host or host == origin}
            ),
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
            {
                "article_id": ident,
                "same_site": not host or host == origin,
                "known_id": ident in available,
            }
            for host, ident in _article_links(reply)
            if ident not in available or (
                host and host != origin and (host, ident) not in self.catalog_references)
        ]
