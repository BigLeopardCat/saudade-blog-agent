"""Retrieval failures and scope must reach model content, not just private artifacts."""

import json
import time
from types import SimpleNamespace
from unittest.mock import patch

import httpx
import pytest
from _memory_support import config
from _rag_store import memory_store, memory_vectors  # noqa: F401
from langchain_core.messages import HumanMessage
from langgraph.checkpoint.memory import InMemorySaver
from openai import OpenAI

from agent.products.blog.application import BlogApplication
from agent.products.blog.capabilities import ToolCatalog
from agent.runtime.checkpoint_conversations import CheckpointConversationStore
from agent.runtime.service import AgentRuntime
from config import settings
from models.llm import get_llm
from rag import search
from rag import vector_index as vectors
from rag.corpus import content_revision
from tools import base, notes


@pytest.fixture
def retrieval(monkeypatch):
    document = {"type": "note", "id": 1, "title": "真实候选", "content": "## 范围\nneedle evidence"}
    document["revision"] = content_revision(document["title"], document["content"])
    index = search.RagIndex(corpus_loader=lambda: [document], vector_warmer=lambda _: None)
    index.build()
    monkeypatch.setattr(search, "_index", index)
    requests = []

    def validate(request):
        requests.append(request)
        assert request.method == "POST" and request.url.path == "/api/public/documents/validate"
        return httpx.Response(
            200,
            json={
                "code": 200,
                "data": {
                    "schemaVersion": 1,
                    "documents": [
                        {
                            "type": "note",
                            "id": 1,
                            "visible": True,
                            "contentRevision": document["revision"],
                        }
                    ],
                },
            },
        )

    with httpx.Client(transport=httpx.MockTransport(validate)) as client:
        monkeypatch.setattr(base, "_client", client)
        yield index, requests


def coverage(result):
    header, _, _ = str(result).partition("\n")
    value = json.loads(header)["retrieval"]
    assert value == result.meta["retrieval"]
    assert value["scope"] == "public_article_body_index" and value["limit"] == 8
    return value


@pytest.mark.parametrize("changed_snapshot", [False, True])
def test_context_expansion_uses_verified_snapshot_without_extra_http(
    retrieval, monkeypatch, changed_snapshot
):
    index, requests = retrieval
    hit = index._lexical_ranked("needle", 8)[0]
    hit["matches"] = [{"section_id": "s1", "offset": 0, "end": 6}]
    monkeypatch.setattr(search, "search", lambda *_, **__: [hit])
    monkeypatch.setattr(search, "active_profile", lambda: SimpleNamespace(count=len))
    monkeypatch.setattr(settings, "rag_context_mode", "parent")
    if changed_snapshot:
        monkeypatch.setattr(index, "_revision", "different-snapshot")
    result = notes.rag_search.invoke({"query": "needle"})
    assert len(requests) == 1
    if changed_snapshot:
        assert result.kind == "unavailable" and result.data is None
        return
    assert result.kind == "ok"
    assert "read_window" not in hit["matches"][0]
    header = json.loads(str(result).partition("\n")[0])
    assert header["context"]["applied"] is not changed_snapshot
    match = result.data[0]["matches"][0]
    assert match["end"] == 6 and match["read_window"]["end"] == 15
    assert result.data[0]["passages"][0]["text"] == "needle evidence"
    assert json.loads(str(result))["documents"][0]["read_requests"][0]["request"]["max_chars"] == 15


@pytest.mark.parametrize(
    "reason",
    [
        "query_embed_failed",
        "warming",
        "stale_view",
        "corpus_changed",
        "missing_vector_database",
        "vector_database_unavailable",
        "vector_dimension_changed",
    ],
)
@pytest.mark.parametrize("query,kind", [("unseen-term", "unavailable"), ("needle", "ok")])
def test_degraded_route_preserves_candidates_but_never_reports_a_complete_negative(
    retrieval, monkeypatch, reason, query, kind
):
    index, requests = retrieval
    monkeypatch.setattr(index, "_vector_ranked", lambda *_, **kwargs: ([], reason, 0))
    result = notes.rag_search.invoke({"query": query})
    assert result.kind == kind and result.meta["rag_reason"] == reason
    state = coverage(result)
    assert state["mode"] == "degraded" and state["semantic"] == "incomplete"
    if kind == "ok":
        expected = index._lexical_ranked(query, 8)
        assert [{k: v for k, v in r.items() if k != "passages"} for r in result.data] == expected
        assert result.data[0]["matches"] and result.data[0]["passages"]
        read = json.loads(str(result))["documents"][0]["read_requests"][0]["request"]
        assert read["section"] == "s1" and read["revision"] == expected[0]["document_revision"]
        assert len(requests) == 1
    else:
        assert not result.data and not requests
        assert "不能据此判断无相关文章" in str(result)


@pytest.mark.parametrize("reason,semantic", [(None, "not_used"), ("no_vector_hits", "completed")])
def test_healthy_negative_is_scoped_to_the_index_and_query(
    retrieval, monkeypatch, reason, semantic
):
    index, requests = retrieval
    monkeypatch.setattr(index, "_vector_ranked", lambda *_, **kwargs: ([], reason, 0))
    result = notes.rag_search.invoke({"query": "unseen-term"})
    assert result.kind == "empty" and not requests
    assert coverage(result)["semantic"] == semantic
    assert "本次查询在当前检索范围内未命中" in str(result)


def test_missing_vectors_make_even_a_successful_vector_query_incomplete(retrieval, monkeypatch):
    index, _ = retrieval
    monkeypatch.setattr(index, "_vector_ranked", lambda *_, **kwargs: ([], "no_vector_hits", 1))
    result = notes.rag_search.invoke({"query": "unseen-term"})
    assert result.kind == "unavailable" and coverage(result)["semantic"] == "incomplete"


@pytest.mark.parametrize("missing,semantic", [(0, "completed"), (1, "incomplete")])
def test_hybrid_candidates_keep_real_route_and_partial_vector_coverage(
    retrieval, monkeypatch, missing, semantic
):
    index, _ = retrieval
    dense = index._lexical_ranked("needle", 8)
    monkeypatch.setattr(index, "_vector_ranked", lambda *_, **kwargs: (dense, None, missing))
    result = notes.rag_search.invoke({"query": "needle"})
    assert result.kind == "ok" and result.data[0]["id"] == 1
    assert result.data[0]["matches"] == dense[0]["matches"]
    assert coverage(result)["mode"] == "hybrid" and coverage(result)["semantic"] == semantic


def test_stale_snapshot_cannot_prove_an_empty_current_corpus(retrieval, monkeypatch):
    index, _ = retrieval
    monkeypatch.setattr(index, "_vector_ranked", lambda *_, **kwargs: ([], None, 0))
    index._dirty, index._last_attempt = True, time.time()
    result = notes.rag_search.invoke({"query": "unseen-term"})
    assert result.kind == "unavailable" and coverage(result)["snapshot_stale"]


@pytest.mark.parametrize("query", ["unseen-term", "needle"])
def test_embedding_503_coverage_survives_the_actual_model_sdk_wire(
    retrieval, monkeypatch, tmp_path, query
):
    index, visibility_requests = retrieval
    for name, value in {
        "embedding_model": "fixture",
        "embedding_api_key": "fixture-key",
        "embedding_base_url": "https://embedding.invalid/v1",
        "embedding_profile": "",
        "embedding_dim": 2,
        "rag_hybrid_enabled": True,
    }.items():
        monkeypatch.setattr(settings, name, value)
    store = memory_store(tmp_path)
    monkeypatch.setattr(vectors, "_store", store)
    monkeypatch.setattr(vectors, "_query_cache", {})
    monkeypatch.setattr(vectors, "_query_order", [])
    with patch.object(vectors, "_embed_texts", return_value=[[1.0, 0.0]]):
        assert store.update(index._chunks, vectors.space_from_settings())["ok"]
    embedding_requests, model_requests = [], []

    def embedding(request):
        embedding_requests.append(request)
        return httpx.Response(503, json={"error": {"message": "synthetic outage"}})

    def model(request):
        body = json.loads(request.content)
        model_requests.append(body)
        delta = (
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "index": 0,
                        "id": "query",
                        "type": "function",
                        "function": {
                            "name": "rag_search",
                            "arguments": json.dumps({"query": query}),
                        },
                    }
                ],
            }
            if len(model_requests) == 1
            else {"role": "assistant", "content": "本轮检索未完整完成。"}
        )
        chunk = {
            "id": "fixture",
            "model": "fixture",
            "object": "chat.completion.chunk",
            "choices": [
                {
                    "index": 0,
                    "delta": delta,
                    "finish_reason": ("tool_calls" if len(model_requests) == 1 else "stop"),
                }
            ],
            "usage": {"prompt_tokens": 20, "completion_tokens": 5, "total_tokens": 25},
        }
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            text="data: " + json.dumps(chunk) + "\n\ndata: [DONE]\n\n",
        )

    with (
        httpx.Client(transport=httpx.MockTransport(embedding)) as embedding_client,
        httpx.Client(transport=httpx.MockTransport(model)) as model_client,
    ):
        sdk = OpenAI(
            api_key="fixture-key",
            base_url="https://embedding.invalid/v1",
            http_client=embedding_client,
            max_retries=0,
        )
        monkeypatch.setattr(vectors, "_get_client", lambda: sdk)
        selected = AgentRuntime(
            CheckpointConversationStore(InMemorySaver()),
            application=BlogApplication(
                ToolCatalog(read_tools={notes.rag_search.name: notes.rag_search})
            ),
            model_factory=lambda **kwargs: get_llm(
                model="fixture",
                api_key="fixture-key",
                base_url="https://model.invalid/v1",
                http_client=model_client,
                **kwargs,
            ),
        )
        result = list(
            selected.stream_events(
                {"messages": [HumanMessage(content="查一下相关公开文章")]}, config()
            )
        )[-1]
    assert len(embedding_requests) == 1 and len(model_requests) == 2
    assert "未完整完成" in result.reply
    observed = next(
        message for message in model_requests[1]["messages"] if message["role"] == "tool"
    )
    assert "artifact" not in observed and "meta" not in observed
    state = json.loads(observed["content"].split("\n")[0])["retrieval"]
    assert state["mode"] == "degraded" and state["semantic"] == "incomplete"
    assert state["scope"] == "public_article_body_index"
    if query == "needle":
        assert len(visibility_requests) == 1
        assert json.loads(observed["content"])["documents"][0]["id"] == 1
    else:
        assert not visibility_requests and "不能据此判断无相关文章" in observed["content"]
