"""Byte-bound source pages remain complete and use an explicit larger transport budget."""

import json
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from rag import corpus
from rag.search import RagIndex
from tools import base
from tools.http_channel import API_RESPONSE_BYTES, CORPUS_RESPONSE_BYTES


def document(identifier, content="body"):
    return {
        "id": identifier,
        "title": "Article",
        "content": content,
        "revision": corpus.content_revision("Article", content),
    }


def page(rows, total, cursor):
    return {
        "schema_version": 1,
        "revision": "a" * 64,
        "total": total,
        "documents": rows,
        "next_cursor": cursor,
    }


def test_short_complete_pages_can_exceed_the_old_assumed_full_page_count(monkeypatch):
    monkeypatch.setattr(corpus, "MAX_DOCUMENTS", 5)
    calls = []

    def get(path):
        params = parse_qs(urlsplit(path).query)
        previous = int(params.get("after", ["0"])[0])
        current = previous + 1
        calls.append(path)
        if previous:
            assert params["revision"] == ["a" * 64]
        return page([document(current)], 5, current if current < 5 else None)

    rows = corpus.fetch(get)
    assert [row["id"] for row in rows] == [1, 2, 3, 4, 5]
    assert len(calls) == 5


@pytest.mark.parametrize("bad", [page([], 2, 1), page([document(1)], 2, 1)])
def test_byte_bounded_paging_still_rejects_empty_or_repeated_cursor_pages(bad):
    with pytest.raises(corpus.CorpusUnavailable):
        corpus.fetch(lambda _: bad)


def test_rag_export_uses_explicit_corpus_transport_capacity(monkeypatch):
    body = "x" * (API_RESPONSE_BYTES // 3 + 1024)
    result = {"code": 200, "data": page([document(i, body) for i in range(1, 4)], 3, None)}
    wire = json.dumps(result).encode()
    assert API_RESPONSE_BYTES < len(wire) < CORPUS_RESPONSE_BYTES
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, content=wire, headers={"content-type": "application/json"})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        monkeypatch.setattr(base, "_client", client)
        rows = RagIndex()._fetch_corpus()
    assert len(rows) == 3 and all(row["content"] == body for row in rows)
    assert len(calls) == 1
    assert calls[0].url.path == "/api/public/rag/corpus"
