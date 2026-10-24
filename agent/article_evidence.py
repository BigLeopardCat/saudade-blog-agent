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


class ArticleEvidence:
    def __init__(self) -> None:
        self.searched = False
        self.found = False
        self.empty = False
        self.failed = False
        self.documents: dict[int, str] = {}

    def observe(self, name: str, args: dict, value) -> None:
        kind = getattr(value, "kind", "ok")
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

    def issue(self, reply: str, site: str) -> str | None:
        if self.found and not self.documents:
            return "article_body_missing"
        if not self.searched and not self.documents:
            return None
        relative = re.findall(r"\]\((/article/\d+)[^)]*\)", reply)
        if any(int(path.rsplit("/", 1)[-1]) not in self.documents for path in relative):
            return "unread_article_reference"
        origin = urlsplit(site).netloc.casefold()
        for url in re.findall(r"https?://[^\s)<>]+", reply):
            parsed = urlsplit(url.rstrip(".,，。"))
            match = re.fullmatch(r"/article/(\d+)/?", parsed.path)
            if match and (
                int(match[1]) not in self.documents or parsed.netloc.casefold() != origin
            ):
                return "unread_article_reference"
        return None

    def sources(self, reply: str, site: str) -> str:
        if not self.documents or re.search(r"/article/\d+", reply):
            return reply
        refs = []
        for article_id, title in self.documents.items():
            label = re.sub(r"[\[\]\r\n]", "", title)[:80]
            refs.append(f"[{label}]({site.rstrip('/')}/article/{article_id})")
        return reply + "\n\n来源：" + "、".join(refs)
