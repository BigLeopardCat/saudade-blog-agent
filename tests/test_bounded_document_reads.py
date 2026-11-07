"""Real read tools consume bounded host ranges and never synthesize successful provenance."""

import json

import httpx
import pytest
from _memory_support import Capture, config
from _parent_repo import read as host_source
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.memory import InMemorySaver
from pydantic import ValidationError

from agent.products.blog.application import BlogApplication
from agent.products.blog.capabilities import ToolCatalog
from agent.runtime.checkpoint_conversations import CheckpointConversationStore
from agent.runtime.service import AgentRuntime
from eval.fixtures.document_reads import document_page
from rag.corpus import content_revision
from rag.sections import PARSER_VERSION, outline, reading_revision
from tools import base
from tools.notes import get_article_detail, get_article_stats


@pytest.fixture
def backend(monkeypatch):
    documents = {
        ("note", 19): ("T", "## Intro\r\n" + "正文🐈 é ![图](/cat.png)\r\n" * 500),
        ("talk", 20): ("说说", "长说说🐈\n" * 700),
        ("board", 21): ("", "长留言🐈\n" * 700),
        ("announcement", 22): ("公告", "长公告🐈\n" * 700),
    }
    calls = []
    changes = {}

    def handle(request):
        calls.append(request)
        parts = request.url.path.split("/")
        if request.method != "GET":
            raise AssertionError("reading cannot mutate data")
        if request.url.path == "/api/public/notes/19/stats":
            return httpx.Response(
                200,
                json={
                    "code": 200,
                    "data": {
                        "views": 10,
                        "likes": 2,
                        "favorites": 3,
                        "comments": 4,
                        "liked": False,
                    },
                },
            )
        if parts[3:4] == ["documents"] and parts[-1] == "read":
            kind, identifier = parts[-3], int(parts[-2])
            source = documents.get((kind, identifier))
            if source is None:
                return httpx.Response(404, json={"code": 404, "message": "not found"})
            params = dict(request.url.params)
            if params.get("revision") and params["revision"] != content_revision(*source):
                return httpx.Response(409, json={"code": 409, "message": "version changed"})
            if params.get("section_schema") and params["section_schema"] != PARSER_VERSION:
                return httpx.Response(409, json={"code": 409, "message": "section view changed"})
            data = document_page(
                kind, identifier, *source, params, {"tags": [], "tagsTruncated": False}
            )
            data.update(changes)
            return httpx.Response(200, json={"code": 200, "data": data})
        raise AssertionError(f"unexpected endpoint: {request.url.path}")

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        monkeypatch.setattr(base, "_client", client)
        yield documents, calls, changes


@pytest.mark.parametrize(
    "kind,identifier", [("note", 19), ("talk", 20), ("board", 21), ("announcement", 22)]
)
def test_all_source_types_roundtrip_long_original_text_with_one_bounded_get_per_page(
    backend, kind, identifier
):
    documents, calls, _ = backend
    params = {"article_id": identifier, "doc_type": kind, "max_chars": 997}
    joined = []
    while True:
        before = len(calls)
        value = get_article_detail.invoke(params)
        assert value.kind == "ok" and len(calls) == before + 1
        data = value.data
        joined.append(data.get("noteContent", data.get("content")))
        assert len(joined[-1]) <= 997
        assert data["contentRevision"] == content_revision(*documents[kind, identifier])
        assert data["source"]["type"] == kind and data["source"]["end"] <= data["totalChars"]
        if data["nextOffset"] is None:
            assert data["readComplete"]
            break
        params.update(
            offset=data["nextOffset"],
            revision=data["contentRevision"],
            section_schema=data["sectionSchema"],
        )
    assert "".join(joined) == documents[kind, identifier][1]
    assert all("/documents/" in request.url.path for request in calls)


def test_stats_is_an_independent_read_without_body_or_view_reporting(backend):
    _, calls, _ = backend
    value = get_article_stats.invoke({"article_id": 19})
    assert value.data == {"noteId": 19, "views": 10, "likes": 2, "favorites": 3, "comments": 4}
    assert [(request.method, request.url.path) for request in calls] == [
        ("GET", "/api/public/notes/19/stats")
    ]


@pytest.mark.parametrize(
    "patch",
    [
        {"id": 20},
        {"type": "board"},
        {"readOffset": True},
        {"readComplete": True},
        {"nextOffset": None},
        {"sectionSchema": "other-parser"},
        {"source": None},
        {
            "source": {
                "type": "note",
                "id": 19,
                "revision": "a" * 64,
                "section_schema": PARSER_VERSION,
                "section_id": "",
                "offset": 0,
                "end": 10,
            }
        },
    ],
)
def test_wrong_identity_range_or_parser_metadata_never_records_body_evidence(backend, patch):
    _, _, changes = backend
    changes.update(patch)
    value = get_article_detail.invoke({"article_id": 19, "max_chars": 5})
    assert value.kind == "unavailable" and value.data is None


@pytest.mark.parametrize(
    "args", [{"article_id": True}, {"article_id": 0}, {"article_id": 19, "max_chars": 24001}]
)
def test_invalid_typed_ids_and_ranges_do_not_make_http_requests(backend, args):
    _, calls, _ = backend
    with pytest.raises(ValidationError):
        get_article_detail.invoke(args)
    assert not calls


def test_stale_revision_and_view_are_explicit_and_missing_locator_has_zero_io(backend, monkeypatch):
    _, calls, _ = backend
    invalidated = []
    monkeypatch.setattr("rag.search.invalidate_document", lambda *args: invalidated.append(args))
    assert get_article_detail.invoke({"article_id": 19, "offset": 5}).kind == "invalid_args"
    assert not calls
    changed = get_article_detail.invoke(
        {"article_id": 19, "offset": 5, "revision": "a" * 64, "section_schema": PARSER_VERSION}
    )
    assert changed.kind == "unavailable" and changed.meta["status"] == 409
    assert invalidated == [(19, "a" * 64)]
    assert get_article_detail.invoke({"article_id": 999}).kind == "not_found"


def test_framework_reads_verified_http_ranges_before_answering_without_hidden_stats(backend):
    documents, calls, _ = backend
    revision = content_revision(*documents["note", 19])
    model = Capture(
        AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "get_article_detail",
                    "id": "first",
                    "args": {"article_id": 19, "max_chars": 5},
                }
            ],
        ),
        AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "get_article_detail",
                    "id": "next",
                    "args": {
                        "article_id": 19,
                        "offset": 5,
                        "revision": revision,
                        "section_schema": PARSER_VERSION,
                        "max_chars": 997,
                    },
                }
            ],
        ),
        AIMessage(content="已读取两段，剩余部分尚未核实。"),
    )
    selected = AgentRuntime(
        CheckpointConversationStore(InMemorySaver()),
        application=BlogApplication(
            ToolCatalog(read_tools={get_article_detail.name: get_article_detail})
        ),
        model_factory=lambda **_: model,
    )
    result = list(
        selected.stream_events({"messages": [HumanMessage(content="读取这篇文章的开头")]}, config())
    )[-1]
    assert result.reply.startswith("已读取两段，剩余部分尚未核实。")
    assert "/article/19" in result.reply
    assert len(model.prompts) == 3 and len(calls) == 2
    assert all(request.method == "GET" and request.url.path.endswith("/read") for request in calls)
    assert selected.store.load(config()).messages[-1].content == result.reply


def test_shared_host_commonmark_fixture_matches_python_rag_source_locations():
    cases = json.loads(host_source("tests/fixtures/document-reading.json"))
    for case in cases:
        actual = [
            {
                "id": row["section_id"],
                "title": row["section"],
                "level": row["level"],
                "path": row["path"],
                "bodyStart": row["body_start"],
                "bodyEnd": row["body_end"],
            }
            for row in outline(case["content"], case["title"])
        ]
        assert actual == case["sections"]
        assert reading_revision(case["title"], case["content"]) == case["contentRevision"]
        assert PARSER_VERSION == case["sectionSchema"]


def test_budget_excerpt_preserves_document_and_section_view_for_continuation(backend):
    from rag.reading import render_article_result

    value = get_article_detail.invoke({"article_id": 19, "max_chars": 12000})
    text, visible = render_article_result(value.data, str(value), 1000)
    assert len(text) <= 1000 and visible["nextOffset"] is not None
    assert visible["contentRevision"] == value.data["contentRevision"]
    assert visible["sectionSchema"] == PARSER_VERSION
    assert visible["source"]["section_schema"] == PARSER_VERSION
    assert visible["source"]["end"] == len(visible["noteContent"])
    assert not visible["readComplete"]
