"""Validated, versioned public corpus snapshots. A failed page never means EOF."""

from __future__ import annotations

import hashlib
import json
from urllib.parse import urlencode

SCHEMA_VERSION = 1
PAGE_SIZE = 50
MAX_DOCUMENTS = 10000


class CorpusUnavailable(RuntimeError):
    """The source did not provide a complete, self-consistent snapshot."""


def content_revision(title: str, content: str) -> str:
    payload = json.dumps([title, content], ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


def corpus_revision(docs: list[dict]) -> str:
    rows = [(d["type"], d["id"], content_revision(d["title"], d["content"])) for d in docs]
    payload = json.dumps(sorted(rows), ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


def _digest(value) -> bool:
    return (
        isinstance(value, str) and len(value) == 64 and all(c in "0123456789abcdef" for c in value)
    )


def fetch(get) -> list[dict]:
    docs: list[dict] = []
    revision = None
    total = 0
    after = 0
    for _ in range(MAX_DOCUMENTS // PAGE_SIZE + 1):
        params: dict[str, int | str] = {"limit": PAGE_SIZE}
        if after and revision is not None:
            params.update(after=after, revision=revision)
        page = get("/rag/corpus?" + urlencode(params))
        if (
            not isinstance(page, dict)
            or page.get("schema_version") != SCHEMA_VERSION
            or not _digest(page.get("revision"))
        ):
            raise CorpusUnavailable("corpus_unavailable_or_invalid_schema")
        if revision is None:
            revision = page["revision"]
            count = page.get("total")
            if type(count) is not int or not 0 <= count <= MAX_DOCUMENTS:
                raise CorpusUnavailable("invalid_corpus_total")
            total = count
        if page["revision"] != revision or page.get("total") != total:
            raise CorpusUnavailable("corpus_revision_changed")
        rows = page.get("documents")
        if not isinstance(rows, list) or len(rows) > PAGE_SIZE:
            raise CorpusUnavailable("invalid_corpus_page")
        for row in rows:
            if (
                not isinstance(row, dict)
                or type(row.get("id")) is not int
                or row["id"] <= after
                or not isinstance(row.get("title"), str)
                or not isinstance(row.get("content"), str)
                or row.get("revision") != content_revision(row["title"], row["content"])
            ):
                raise CorpusUnavailable("invalid_or_out_of_order_document")
            after = row["id"]
            docs.append({"type": "note", **row})
        cursor = page.get("next_cursor")
        if cursor is None:
            if len(docs) != total:
                raise CorpusUnavailable("incomplete_corpus")
            return docs
        if not rows or type(cursor) is not int or cursor != after or len(docs) >= total:
            raise CorpusUnavailable("invalid_corpus_cursor")
    raise CorpusUnavailable("corpus_limit_exceeded")
