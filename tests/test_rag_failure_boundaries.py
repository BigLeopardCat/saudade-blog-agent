"""Real SDK retries, wire accounting and source locators stay explicit and bounded."""

import copy
import json
import time
from threading import Event

import httpx
import pytest
from _rag_store import memory_store, memory_vectors  # noqa: F401

from agent.runtime.control import AgentCancelled, RequestControl, request_scope
from config import settings
from rag import vector_index as vectors
from tools.notes import _rag_hit


@pytest.fixture
def wire(monkeypatch, tmp_path):
    import openai

    for key, value in {
        "embedding_model": "fixture", "embedding_api_key": "fixture-key",
        "embedding_base_url": "https://embedding.invalid/v1", "embedding_timeout": 3,
        "embedding_profile": "", "embedding_dim": 2, "embedding_batch_size": 1,
    }.items():
        monkeypatch.setattr(settings, key, value)
    monkeypatch.setattr(vectors, "_client", None)
    monkeypatch.setattr(vectors, "_client_sig", None)
    clients, requests = [], []

    def install(respond):
        def transport(request):
            requests.append(request)
            texts = json.loads(request.content)["input"]
            status = respond(texts, len(requests))
            body = {"object": "list", "model": "fixture", "data": [
                {"index": index, "object": "embedding", "embedding": [1.0, 0.0]}
                for index in range(len(texts))
            ]} if status == 200 else {"error": {"message": "synthetic endpoint rejection"}}
            return httpx.Response(status, json=body, request=request)

        def client(**options):
            value = httpx.Client(transport=httpx.MockTransport(transport), **options)
            clients.append(value)
            return value

        monkeypatch.setattr(openai, "DefaultHttpxClient", client)
        return requests

    yield install
    for client in clients:
        client.close()


@pytest.mark.parametrize("status", [401, 403, 404, 408, 409, 429, 500, 503])
def test_endpoint_rejections_never_retry_per_input_or_continue_pending_batches(wire, status):
    requests = wire(lambda texts, count: status)
    assert vectors._embed_texts(["first", "second", "third"], vectors.space_from_settings()) == [
        None, None, None]
    assert len(requests) == 1
    assert vectors._get_client().max_retries == 0


def test_single_oversized_input_is_not_sent_twice(wire):
    requests = wire(lambda texts, count: 413)
    assert vectors._embed_texts(["one"], vectors.space_from_settings()) == [None]
    assert len(requests) == 1


def test_only_multi_input_413_is_split_and_actual_wire_count_is_persisted(wire, tmp_path,
                                                                        monkeypatch):
    requests = wire(lambda texts, count: 413 if len(texts) > 1 else 200)
    monkeypatch.setattr(settings, "embedding_batch_size", 2)
    store = memory_store(tmp_path)
    chunks = [{"type": "note", "id": index, "title": "T", "section": "S", "text": text}
              for index, text in enumerate(["first", "second"], 1)]
    result = store.update(chunks, vectors.space_from_settings())
    assert result["ok"] and result["api_calls"] == len(requests) == 3
    assert result["logical_batches"] == 1
    stats = store.repository.snapshot().stats
    assert stats["api_calls"] == 3 and stats["logical_batches"] == 1
    repeated = store.update(chunks, vectors.space_from_settings())
    assert repeated["api_calls"] == repeated["logical_batches"] == 0
    assert len(requests) == 3


def test_outage_after_splitting_stops_remaining_inputs(wire, monkeypatch):
    requests = wire(lambda texts, count: 413 if count == 1 else 503 if count == 3 else 200)
    monkeypatch.setattr(settings, "embedding_batch_size", 3)
    result = vectors._embed_texts(["first", "second", "third", "fourth"],
                                 vectors.space_from_settings())
    assert result == [[1.0, 0.0], None, None, None] and len(requests) == 3


def test_a_successful_prefix_survives_a_later_endpoint_outage(wire):
    requests = wire(lambda texts, count: 200 if count == 1 else 503)
    assert vectors._embed_texts(["first", "second", "third"],
                               vectors.space_from_settings()) == [[1.0, 0.0], None, None]
    assert len(requests) == 2


def test_failed_initial_build_reports_real_attempts_without_publishing_partial_membership(wire,
                                                                                      tmp_path):
    requests = wire(lambda texts, count: 503)
    store = memory_store(tmp_path)
    chunks = [{"type": "note", "id": index, "title": "T", "section": "S", "text": str(index)}
              for index in range(1, 4)]
    result = store.update(chunks, vectors.space_from_settings())
    assert result["ok"] is False and result["api_calls"] == len(requests) == 1
    assert result["logical_batches"] == result["missing"] == 3
    assert store.repository.snapshot() is None


def test_transport_timeout_does_not_retry_the_batch(wire, monkeypatch):
    def timeout(texts, count):
        raise httpx.ReadTimeout("synthetic timeout")

    requests = wire(timeout)
    monkeypatch.setattr(settings, "embedding_batch_size", 2)
    assert vectors._embed_texts(["first", "second"], vectors.space_from_settings()) == [None, None]
    assert len(requests) == 1


def test_request_deadline_bounds_the_sdk_timeout_and_cancel_is_not_converted_to_fallback(wire):
    requests = wire(lambda texts, count: 200)
    stop = Event()
    with request_scope(RequestControl(stop, time.monotonic() + 0.5)):
        assert vectors._embed_texts(["one"], vectors.space_from_settings()) == [[1.0, 0.0]]
        assert 0 < requests[0].extensions["timeout"]["read"] <= 0.5
        stop.set()
        with pytest.raises(AgentCancelled):
            vectors._embed_texts(["cancelled"], vectors.space_from_settings())
    assert len(requests) == 1


def test_rrf_keeps_duplicate_heading_titles_bound_to_their_own_section_ranges():
    common = {"type": "note", "id": 1, "title": "Synthetic", "document_revision": "rev"}
    lexical = {**common, "sections": ["配置", "配置"], "section_ids": ["s1", "s2"],
               "matches": [{"section_id": ident, "offset": offset, "end": offset + 10}
                           for ident, offset in [("s1", 10), ("s2", 20)]]}
    dense = {**common, "sections": ["后续"], "section_ids": ["s3"],
             "matches": [{"section_id": "s3", "offset": 30, "end": 40}]}
    originals = copy.deepcopy((lexical, dense))
    result = vectors.rrf_fuse([lexical], [dense], 60, 3)[0]
    assert [(loc["section"], loc["section_id"]) for loc in result["locators"]] == [
        ("配置", "s1"), ("配置", "s2"), ("后续", "s3")]
    assert list(zip(result["sections"], result["section_ids"])) == [("配置", "s1"), ("配置", "s2")]
    assert [(match["section_id"], match["offset"]) for match in result["matches"]] == [
        ("s1", 10), ("s2", 20), ("s3", 30)]
    assert "后续 section=s2" not in _rag_hit(1, result)
    assert (lexical, dense) == originals
