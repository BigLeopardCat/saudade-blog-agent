"""Article-body evidence collected by native tool sessions."""

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
        self.candidates: dict[int, str] = {}
        self.references: set[tuple[str, int]] = set()

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
            if body_text(data).strip() and type(args.get("article_id")) is int:
                self.documents[args["article_id"]] = str((data or {}).get("noteTitle") or "文章")
                self.references.update(_article_links(body_text(data)))

    def issue(self, reply: str, site: str) -> str | None:
        if self.found and not self.documents and not self.candidates:
            return "article_body_missing"
        if not self.searched and not self.documents and not self.candidates:
            return None
        origin = urlsplit(site).netloc.casefold()
        referenced = {ident for host, ident in self.references if not host or host == origin}
        available = {*self.documents, *self.candidates, *referenced}
        for host, ident in _article_links(reply):
            if ident not in available or host and host != origin:
                return "unread_article_reference"
        return None

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
