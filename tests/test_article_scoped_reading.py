"""Known-document retrieval, complete outlines and original-source reading costs."""

import json
import re

import httpx
import pytest
from _memory_support import config
from _rag_store import memory_vectors  # noqa: F401
from langchain_core.messages import HumanMessage
from langgraph.checkpoint.memory import InMemorySaver

from agent.domains.articles.evidence import ArticleEvidence
from agent.products.blog.application import BlogApplication
from agent.products.blog.capabilities import ToolCatalog
from agent.runtime.checkpoint_conversations import CheckpointConversationStore
from agent.runtime.service import AgentRuntime
from models.llm import get_llm
from rag import search
from rag.corpus import content_revision
from rag.reading import candidate_text, render_article_result
from rag.sections import PARSER_VERSION
from tools import base, notes

ARTICLE = 41
QUERY = "持久调度归还槽位"
FACT = "该方案以事务记录提交结果，工作线程退出后归还并发槽位。"


@pytest.fixture
def article(monkeypatch):
    body = "".join(
        f"## {'重复标题' if i in (2, 22) else '章节' + str(i)}\n"
        + (
            ("陶瓷纹理青花工艺。" * 3300 + "\n" + QUERY + "：" + FACT)
            if i == 22
            else "青花瓷器图案。" * 90
        )
        + "\n"
        for i in range(1, 26)
    )
    document = {"type": "note", "id": ARTICLE, "title": "固定架构材料", "content": body}
    document["revision"] = content_revision(document["title"], body)
    crowd = [
        {
            "type": "note",
            "id": i,
            "title": QUERY,
            "content": QUERY * 20,
            "revision": content_revision(QUERY, QUERY * 20),
        }
        for i in range(42, 61)
    ]
    index = search.RagIndex(corpus_loader=lambda: [document, *crowd], vector_warmer=lambda _: None)
    index.build()
    monkeypatch.setattr(search, "_index", index)
    calls = []

    def handle(request):
        calls.append(request)
        if request.url.path == "/api/public/documents/validate":
            assert request.method == "POST"
            references = json.loads(request.content)["documents"]
            return httpx.Response(
                200,
                json={
                    "code": 200,
                    "data": {
                        "schemaVersion": 1,
                        "documents": [
                            {
                                "type": ref["type"],
                                "id": ref["id"],
                                "visible": True,
                                "contentRevision": ref["revision"],
                            }
                            for ref in references
                        ],
                    },
                },
            )
        assert request.method == "GET"
        assert request.url.path == f"/api/public/documents/note/{ARTICLE}/read"
        params = dict(request.url.params)
        if params.get("revision") and params["revision"] != document["revision"]:
            return httpx.Response(409)
        if params.get("section_schema") and params["section_schema"] != PARSER_VERSION:
            return httpx.Response(409)
        from eval.fixtures.document_reads import document_page

        try:
            page = document_page("note", ARTICLE, document["title"], body, params)
        except ValueError:
            return httpx.Response(400)
        return httpx.Response(200, json={"code": 200, "data": page})

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        monkeypatch.setattr(base, "_client", client)
        yield document, index, calls


def locator(hit):
    match = next(row for row in hit["matches"] if row["section_id"] == "s22")
    return {
        "article_id": ARTICLE,
        "section": "s22",
        "offset": match["offset"],
        "max_chars": match["end"] - match["offset"],
        "revision": hit["document_revision"],
        "section_schema": hit["section_schema"],
    }


def test_article_filter_precedes_the_global_document_window(article):
    _, index, _ = article
    assert index._lexical_ranked(QUERY, 1)[0]["id"] != ARTICLE
    scoped = index._lexical_ranked(QUERY, 1, article_id=ARTICLE)
    assert [row["id"] for row in scoped] == [ARTICLE]
    assert "s22" in scoped[0]["section_ids"]
    result = notes.rag_search.invoke({"query": QUERY, "top_k": 1, "article_id": ARTICLE})
    assert result.kind == "ok" and [row["id"] for row in result.data] == [ARTICLE]
    coverage = json.loads(str(result).split("\n")[0])["retrieval"]
    assert coverage["article_id"] == ARTICLE and "section=s22" in str(result)
    assert index._lexical_ranked(QUERY, 1, article_id=999) == []


def test_direct_locator_reads_the_deep_match_and_not_the_start_of_a_long_section(article):
    _, index, _ = article
    hit = index._lexical_ranked(QUERY, 1, article_id=ARTICLE)[0]
    arguments = locator(hit)
    assert arguments["offset"] > 24000 and arguments["max_chars"] < 2000
    wire = candidate_text(1, hit)
    assert f"offset={arguments['offset']}" in wire
    assert f"max_chars={arguments['max_chars']}" in wire
    result = notes.get_article_detail.invoke(arguments)
    assert result.kind == "ok" and FACT in result.data["sectionText"]
    assert len(result.data["sectionText"]) == arguments["max_chars"]
    assert result.data["source"]["offset"] == arguments["offset"]


def test_shared_candidate_version_can_read_every_visible_section(article):
    _, index, _ = article
    hit = index._lexical_ranked("青花瓷器图案", 1, article_id=ARTICLE)[0]
    wire = candidate_text(1, hit)
    revision = re.findall(r"\brevision=(\S+)", wire)
    schema = re.findall(r"\bsection_schema=(\S+)", wire)
    assert revision == [hit["document_revision"]] and schema == [PARSER_VERSION]
    ranges = re.findall(r"\bsection=(s\d+) offset=(\d+) max_chars=(\d+)", wire)
    assert len(ranges) == len(hit["matches"]) > 1
    for section, offset, length in ranges:
        result = notes.get_article_detail.invoke({
            "article_id": ARTICLE, "section": section, "offset": int(offset),
            "max_chars": int(length), "revision": revision[0], "section_schema": schema[0],
        })
        assert result.kind == "ok" and result.data["sectionText"]
        assert result.data["source"]["offset"] == int(offset)
        assert len(result.data["sectionText"]) == int(length)
        assert result.data["omittedViews"] == ["metadata", "outline"]
        assert not {"sections", "sectionCount", "sectionsOffset", "nextSectionsOffset",
                    "authorName", "description", "tags"}.intersection(result.data)


def test_every_directory_entry_can_be_traversed_without_claiming_body_reads(article):
    _, _, calls = article
    offset, revision, evidence, identifiers = 0, "", ArticleEvidence(), []
    while True:
        args = {"article_id": ARTICLE, "view": "outline", "offset": offset}
        if revision:
            args.update(revision=revision, section_schema=PARSER_VERSION)
        result = notes.get_article_detail.invoke(args)
        assert result.kind == "ok"
        data = result.data
        assert data["view"] == "outline" and data["source"] is None
        assert data["sectionsOffset"] == offset and not data["readComplete"]
        assert data["readOffset"] == 0 and data["nextOffset"] is None
        assert not any(data.get(key) for key in ("noteContent", "sectionText", "content"))
        evidence.observe("get_article_detail", args, result)
        identifiers.extend(row["id"] for row in data["sections"])
        revision, offset = data["contentRevision"], data["nextSectionsOffset"]
        if offset is None:
            break
    assert identifiers == [f"s{i}" for i in range(1, 26)]
    assert len(calls) == 3 and evidence.documents == {} and evidence.read_ranges == {}
    assert ARTICLE in evidence.candidates


def test_outline_cursors_require_the_view_and_matching_version(article):
    document, _, calls = article
    assert (
        notes.get_article_detail.invoke(
            {"article_id": ARTICLE, "view": "outline", "offset": 12}
        ).kind
        == "invalid_args"
    )
    assert calls == []
    assert (
        notes.get_article_detail.invoke(
            {"article_id": ARTICLE, "view": "outline", "section": "s22"}
        ).kind
        == "invalid_args"
    )
    stale = notes.get_article_detail.invoke(
        {
            "article_id": ARTICLE,
            "view": "outline",
            "offset": 12,
            "revision": "0" * 64,
            "section_schema": PARSER_VERSION,
        }
    )
    assert stale.kind == "unavailable" and stale.meta["status"] == 409
    wrong_cursor = notes.get_article_detail.invoke(
        {
            "article_id": ARTICLE,
            "view": "outline",
            "offset": 12000,
            "revision": document["revision"],
            "section_schema": PARSER_VERSION,
        }
    )
    assert wrong_cursor.kind == "invalid_args"


def test_outline_budgeting_returns_parseable_pages_and_never_skips_unseen_entries(article):
    _, _, _ = article
    raw = notes.get_article_detail.invoke({"article_id": ARTICLE, "view": "outline"})
    cap = len(str(raw)) - 150
    text, visible = render_article_result(raw.data, str(raw), cap)
    assert visible == json.loads(text) and len(text) <= cap
    assert 0 < len(visible["sections"]) < len(raw.data["sections"])
    assert visible["nextSectionsOffset"] == len(visible["sections"])
    assert visible["noteId"] == ARTICLE
    following = notes.get_article_detail.invoke(
        {
            "article_id": ARTICLE,
            "view": "outline",
            "offset": visible["nextSectionsOffset"],
            "revision": visible["contentRevision"],
            "section_schema": visible["sectionSchema"],
        }
    )
    assert following.data["sections"][0]["id"] == f"s{len(visible['sections']) + 1}"
    text, visible = render_article_result(raw.data, str(raw), 10)
    assert visible is None and len(text) <= 10


def test_real_sdk_searches_the_known_article_then_reads_only_the_evidence(article, monkeypatch):
    _, index, calls = article
    arguments = locator(index._lexical_ranked(QUERY, 1, article_id=ARTICLE)[0])
    requests = []
    actions = [
        ("rag_search", {"query": QUERY, "top_k": 1, "article_id": ARTICLE}),
        ("get_article_detail", arguments),
    ]

    def model(request):
        body = json.loads(request.content)
        requests.append(body)
        position = len(requests) - 1
        if position < len(actions):
            name, args = actions[position]
            delta = {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "index": 0,
                        "id": f"call{position}",
                        "type": "function",
                        "function": {"name": name, "arguments": json.dumps(args)},
                    }
                ],
            }
        else:
            delta = {
                "role": "assistant",
                "content": FACT + f" 来源：[固定材料](/article/{ARTICLE})",
            }
        chunk = {
            "id": "test",
            "model": "test",
            "object": "chat.completion.chunk",
            "choices": [
                {
                    "index": 0,
                    "delta": delta,
                    "finish_reason": "tool_calls" if position < len(actions) else "stop",
                }
            ],
            "usage": {"prompt_tokens": 20, "completion_tokens": 5, "total_tokens": 25},
        }
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            text="data: " + json.dumps(chunk) + "\n\ndata: [DONE]\n\n",
        )

    with httpx.Client(transport=httpx.MockTransport(model)) as client:
        runtime = AgentRuntime(
            CheckpointConversationStore(InMemorySaver()),
            application=BlogApplication(
                ToolCatalog(
                    read_tools={
                        tool.name: tool for tool in (notes.rag_search, notes.get_article_detail)
                    }
                )
            ),
            model_factory=lambda **kwargs: get_llm(
                model="test",
                api_key="test-only",
                base_url="https://model.invalid/v1",
                http_client=client,
                **kwargs,
            ),
        )
        result = list(
            runtime.stream_events(
                {"messages": [HumanMessage(content=f"查文章{ARTICLE}里关于{QUERY}的原始说明")]},
                config(),
            )
        )[-1]
    assert FACT in result.reply and len(requests) == 3 and len(calls) == 2
    retrieval = next(
        message["content"] for message in requests[1]["messages"] if message["role"] == "tool"
    )
    assert "section=s22" in retrieval and "max_chars=" in retrieval
    body = json.loads(
        next(
            message["content"]
            for message in requests[2]["messages"]
            if message["role"] == "tool" and "sectionText" in message["content"]
        )
    )
    assert FACT in body["sectionText"] and len(body["sectionText"]) < 2000
    parent = runtime.get_state(config(), subgraphs=True)
    assert parent.values["messages"][-1].content == result.reply
    checkpoints = [
        saved.checkpoint["channel_values"]
        for saved in runtime.saver.list(config())
        if saved.config["configurable"].get("checkpoint_ns")
    ]
    assert any(
        ARTICLE in state.get("evidence", {}).get("candidates", {})
        and not state["evidence"]["read_ranges"]
        for state in checkpoints
    )
    native = [state for state in checkpoints if state.get("evidence", {}).get("read_ranges")]
    assert native, "native ArticleState must checkpoint visible source ranges"
    ranges = native[0]["evidence"]["read_ranges"][ARTICLE]
    assert sum(row["visible_chars"] for row in ranges) < 2000
    observed = next(
        message
        for message in native[0]["messages"]
        if message.type == "tool" and message.name == "get_article_detail"
    )
    assert observed.artifact["article_evidence"]["read_ranges"][ARTICLE] == ranges


def test_body_outline_mismatch_and_broken_directory_cursors_are_unavailable(article, monkeypatch):
    document, _, _ = article
    from eval.fixtures.document_reads import document_page

    raw = document_page(
        "note", ARTICLE, document["title"], document["content"], {"view": "outline"}
    )
    for malformed in (
        {"nextSectionsOffset": None},
        {"readOffset": 12},
        {"view": "body"},
        {"sectionsOffset": 12},
    ):
        monkeypatch.setattr(notes, "_get", lambda *args, **kwargs: {**raw, **malformed})
        result = notes.get_article_detail.invoke({"article_id": ARTICLE, "view": "outline"})
        assert result.kind == "unavailable" and result.data is None


def test_lexical_degradation_retains_the_requested_article_scope(article, monkeypatch):
    _, index, _ = article
    observed = []

    def unavailable(query, top_k, *, article_id=None):
        observed.append(article_id)
        return [], "vector_database_unavailable", 0

    monkeypatch.setattr(index, "_vector_ranked", unavailable)
    result = notes.rag_search.invoke({"query": QUERY, "top_k": 1, "article_id": ARTICLE})
    assert result.kind == "ok" and observed == [ARTICLE]
    assert {row["id"] for row in result.data} == {ARTICLE}
    assert result.meta["retrieval"]["article_id"] == ARTICLE
    assert result.meta["retrieval"]["semantic"] == "incomplete"


def test_third_lexical_section_retains_a_revision_bound_executable_locator(monkeypatch):
    document = {
        "type": "note",
        "id": ARTICLE,
        "title": "Three matches",
        "content": "## first\nneedle one\n## second\nneedle two\n## third\nneedle three",
    }
    index = search.RagIndex(corpus_loader=lambda: [document], vector_warmer=lambda _: None)
    index.build()
    hit = index._lexical_ranked("needle", 1, article_id=ARTICLE)[0]
    assert len(hit["sections"]) == len(hit["section_ids"]) == 3
    wire = candidate_text(1, hit)
    for section in ("s1", "s2", "s3"):
        assert f"section={section}" in wire
    assert f"revision={hit['document_revision']}" in wire
    assert f"section_schema={PARSER_VERSION}" in wire and "max_chars=" in wire


def test_a_revision_mismatch_during_fusion_keeps_scoped_lexical_results(article, monkeypatch):
    _, index, _ = article
    hit = index._lexical_ranked(QUERY, 1, article_id=ARTICLE)[0]
    monkeypatch.setattr(index, "_vector_ranked", lambda *args, **kwargs: (
        [{**hit, "document_revision": "0" * 64}], None, 0))
    result = notes.rag_search.invoke({"query": QUERY, "top_k": 1, "article_id": ARTICLE})
    assert result.kind == "ok" and result.data == [hit]
    assert result.meta["rag_reason"] == "corpus_changed"
    assert result.meta["retrieval"]["semantic"] == "incomplete"
    assert result.meta["retrieval"]["article_id"] == ARTICLE
