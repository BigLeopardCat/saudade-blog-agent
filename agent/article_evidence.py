"""Article-body evidence shared by native sessions and graph completion checks."""

from __future__ import annotations

import ast
import re
from urllib.parse import urlsplit

from langchain_core.messages import ToolMessage

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


def needs_article_body(messages: list) -> bool:
    """Graph completion must not treat candidate rows as article body evidence."""
    found = False
    for message in messages:
        if not isinstance(message, ToolMessage):
            continue
        name = getattr(message, "name", "")
        value = str(message.content)
        if name == "rag_search" and re.search(r"^\s*\d+\.\s*type=note\s+id=", value, re.M):
            found = True
        if name == "get_article_detail":
            data = payload(value)
            if data and (data.get("noteId") or data.get("noteKey")) and body_text(data).strip():
                return False
    return found


def completion_issue(plan: dict, messages: list) -> str | None:
    if plan.get("workflow") in ("content_query", "read_article") and needs_article_body(messages):
        return "article_body_missing"
    return None


def completion_read_plan(plan: dict, messages: list, executed: list):
    if plan.get("tools") or not completion_issue(plan, messages):
        return None
    from agent.deterministic_plan import _candidate_detail_plan

    return _candidate_detail_plan(messages, executed, set())
