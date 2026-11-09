"""Source authority is checked before cached retrieval candidates reach the model."""

import copy
import json

import httpx
import pytest
from langchain_core.messages import AIMessage, HumanMessage

from agent.products.blog.articles import ArticleMiddleware
from agent.runtime.session import ToolSession
from rag import search
from rag.corpus import content_revision
from rag.visibility import VisibilityUnavailable, current_public
from tools import base, notes


def candidate(ident, revision="a" * 64):
    return {"type": "note", "id": ident, "title": f"candidate-{ident}",
            "content_revision": revision, "document_revision": "f" * 64}


def test_batch_validation_uses_source_hash_and_identity_not_row_order_or_read_view_hash():
    rows = [candidate(1), candidate(2, "b" * 64)]
    requests = []

    def get(path, *, payload):
        requests.append((path, payload))
        return {"schemaVersion": 1, "documents": [
            {"type": "note", "id": row["id"], "visible": True,
             "contentRevision": row["content_revision"]} for row in reversed(rows)]}

    result = current_public(rows, get)
    assert result.current == rows and not result.changed
    assert requests == [("/documents/validate", {"documents": [
        {"type": "note", "id": 1, "revision": "a" * 64},
        {"type": "note", "id": 2, "revision": "b" * 64}]})]


@pytest.mark.parametrize("rows", [
    [candidate(1), candidate(1)], [candidate(True)], [candidate(-1)],
    [candidate(2_147_483_648)], [{**candidate(1), "type": "message"}],
    [{**candidate(1), "content_revision": None}],
    [{key: value for key, value in candidate(1).items() if key != "content_revision"}],
    [candidate(ident) for ident in range(1, 66)],
])
def test_invalid_or_unversioned_candidates_never_trigger_a_source_request(rows):
    def forbidden(*args, **kwargs):
        pytest.fail("invalid candidate reached source lookup")

    with pytest.raises(VisibilityUnavailable):
        current_public(rows, forbidden)


@pytest.mark.parametrize("fault", ["missing", "duplicate", "extra", "wrong_type", "bool_schema",
                                    "string_visible", "visible_without_hash", "hidden_hash",
                                    "hidden_title", "bad_hash", "missing_hash"])
def test_partial_ambiguous_or_leaking_source_receipts_do_not_authorize_candidates(fault):
    response = {"schemaVersion": 1, "documents": [
        {"type": "note", "id": ident, "visible": True, "contentRevision": "a" * 64}
        for ident in [1, 2]]}
    rows = response["documents"]
    if fault == "missing":
        rows.pop()
    elif fault == "duplicate":
        rows[1] = dict(rows[0])
    elif fault == "extra":
        rows.append({**rows[0], "id": 3})
    elif fault == "wrong_type":
        rows[0]["type"] = "message"
    elif fault == "bool_schema":
        response["schemaVersion"] = True
    elif fault == "string_visible":
        rows[0]["visible"] = "true"
    elif fault == "visible_without_hash":
        rows[0]["contentRevision"] = None
    elif fault == "hidden_hash":
        rows[0]["visible"] = False
    elif fault == "hidden_title":
        rows[0].update(visible=False, contentRevision=None, title="must remain hidden")
    elif fault == "bad_hash":
        rows[0]["contentRevision"] = "not a revision"
    elif fault == "missing_hash":
        rows[0].pop("contentRevision")
    with pytest.raises(VisibilityUnavailable):
        current_public([candidate(1), candidate(2)], lambda *args, **kwargs: response)


@pytest.fixture
def backend(monkeypatch):
    documents = [{"type": "note", "id": ident, "title": f"source-title-{ident}",
                  "content": "needle source content"} for ident in [1, 2]]
    for doc in documents:
        doc["revision"] = content_revision(doc["title"], doc["content"])
    index = search.RagIndex(corpus_loader=lambda: documents, vector_warmer=lambda _: None)
    index.build()
    monkeypatch.setattr(search, "_index", index)
    monkeypatch.setattr(search, "is_enabled", lambda: False)
    states = [{"type": "note", "id": doc["id"], "visible": True,
               "contentRevision": doc["revision"]} for doc in documents]
    requests, clients = [], []

    def install(*, rows=None, status=200):
        def reply(request):
            requests.append(request)
            assert request.method == "POST" and request.url.path == "/api/public/documents/validate"
            assert "authorization" not in request.headers
            assert all(set(row) == {"type", "id", "revision"}
                       for row in json.loads(request.content)["documents"])
            return httpx.Response(status, json={"code": status, "data": {
                "schemaVersion": 1, "documents": states if rows is None else rows}})

        client = httpx.Client(transport=httpx.MockTransport(reply))
        clients.append(client)
        monkeypatch.setattr(base, "_client", client)
        return index, states, requests

    yield install
    for client in clients:
        client.close()


@pytest.mark.parametrize("role", ["user", "superadmin"])
def test_public_retrieval_does_not_inherit_a_callers_right_to_hidden_articles(backend, role):
    index, rows, requests = backend()
    rows[0].update(visible=False, contentRevision=None)
    result = notes.rag_search.invoke({"query": "needle"},
                                    config={"configurable": {"role": role, "user_id": 7}})
    assert result.kind == "ok" and [row["id"] for row in result.data] == [2]
    assert "source-title-1" not in str(result)
    assert result.meta["corpus"]["stale"] and result.meta["visibility_dropped"] == 1
    assert index.status()["stale"] and len(requests) == 1


def test_a_repeated_query_rechecks_public_state_instead_of_reusing_the_authorization(backend):
    _, rows, requests = backend()
    assert [row["id"] for row in notes.rag_search.invoke({"query": "needle"}).data] == [1, 2]
    rows[0].update(visible=False, contentRevision=None)
    result = notes.rag_search.invoke({"query": "needle"})
    assert result.kind == "ok" and [row["id"] for row in result.data] == [2]
    assert len(requests) == 2


def test_changed_source_hash_excludes_old_matches_and_marks_the_index_dirty(backend):
    index, rows, requests = backend()
    rows[0]["contentRevision"] = content_revision("new title", "new content")
    result = notes.rag_search.invoke({"query": "needle"})
    assert result.kind == "ok" and [row["id"] for row in result.data] == [2]
    assert index.status()["stale"] and len(requests) == 1


def test_all_revoked_candidates_mean_the_index_changed_not_a_complete_zero_match_search(backend):
    index, rows, requests = backend()
    for row in rows:
        row.update(visible=False, contentRevision=None)
    result = notes.rag_search.invoke({"query": "needle"})
    assert result.kind == "unavailable" and result.meta["reason"] == "corpus_changed"
    assert result.data is None and "source-title" not in str(result)
    assert index.status()["stale"] and len(requests) == 1


def test_source_failure_cannot_expose_cached_titles_or_claim_empty_matches(backend):
    backend(status=500)
    result = notes.rag_search.invoke({"query": "needle"})
    assert result.kind == "unavailable" and result.data is None
    assert result.meta["reason"] == "source_visibility_unavailable"
    assert "source-title" not in str(result)


def test_missing_cached_source_revision_requests_a_rebuild_without_reading_the_source(backend,
                                                                                    monkeypatch):
    index, _, requests = backend()
    for chunk in index._chunks:
        chunk.pop("revision")
    rebuilds = []
    monkeypatch.setattr(search, "warm_async", lambda: rebuilds.append(True))
    result = notes.rag_search.invoke({"query": "needle"})
    assert result.kind == "unavailable" and result.meta["reason"] == "missing_source_revision"
    assert rebuilds == [True] and not requests


@pytest.mark.parametrize("top_k", [True, 0, -1, 51, 64, 65])
def test_tool_candidate_count_is_bounded_before_retrieval(top_k):
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        notes.rag_search.invoke({"query": "needle", "top_k": top_k})


def test_true_empty_retrieval_does_not_invent_a_visibility_lookup(backend):
    _, _, requests = backend()
    assert notes.rag_search.invoke({"query": "absent-token"}).kind == "empty"
    assert not requests


def test_standard_model_tool_loop_receives_only_a_safe_failure_after_source_revocation(backend):
    _, rows, _ = backend()
    for row in rows:
        row.update(visible=False, contentRevision=None)
    replies = iter([
        AIMessage(content="", tool_calls=[{"name": "rag_search", "args": {"query": "needle"},
                                          "id": "retrieval"}]),
        AIMessage(content="索引公开版本发生变化，本次尚未取得可靠候选。"),
    ])
    prompts = []

    class Model:
        def bind_tools(self, tools, **kwargs):
            from _budget_model import schemas
            self.kwargs = {"tools": schemas(tools)}
            return self

        def invoke(self, prompt, **kwargs):
            prompts.append(copy.copy(prompt))
            return next(replies)

    result = ToolSession(Model(), {}, {"rag_search": notes.rag_search}, {},
                         middleware=(ArticleMiddleware,)).run([HumanMessage(content="找文章")])
    assert len(prompts) == 2 and result.reply == "索引公开版本发生变化，本次尚未取得可靠候选。"
    observation = next(message for message in prompts[-1] if message.type == "tool")
    assert observation.status == "error" and "source-title" not in observation.content
