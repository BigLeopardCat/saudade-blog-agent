"""Complete inputs, source coverage and production vector fallback, without network."""

import hashlib
import json

import httpx
import pytest
from openai import OpenAI
from tokenizers import Tokenizer, models, pre_tokenizers, processors

from config import settings
from rag import embedding_inputs as inputs
from rag import search
from rag import vector_index as vectors
from rag.chunks import split
from rag.corpus import content_revision


@pytest.fixture
def profile(tmp_path, monkeypatch):
    unknown_symbol = "[UNK]"
    tokenizer = Tokenizer(
        models.WordLevel({unknown_symbol: 0, "[BOS]": 1, "[EOS]": 2}, unk_token=unknown_symbol)
    )
    tokenizer.pre_tokenizer = pre_tokenizers.Split("", behavior="isolated")
    tokenizer.post_processor = processors.TemplateProcessing(
        single="[BOS] $A [EOS]", special_tokens=[("[BOS]", 1), ("[EOS]", 2)]
    )
    tokenizer.enable_truncation(max_length=4)  # Loader must disable baked-in truncation/padding.
    tokenizer.enable_padding(length=4)
    raw = tokenizer.to_str().encode()
    (tmp_path / "tokenizer.json").write_bytes(raw)
    spec = {
        "schema_version": 1,
        "model_revision": "fixture-weights-q8",
        "tokenizer_file": "tokenizer.json",
        "tokenizer_sha256": hashlib.sha256(raw).hexdigest(),
        "max_tokens": 64,
        "query_prefix": "Q: ",
        "document_prefix": "D: ",
        "pooling": "last",
        "normalization": "L2",
    }
    path = tmp_path / "profile.json"
    path.write_text(json.dumps(spec))
    monkeypatch.setattr(settings, "embedding_profile", str(path))
    monkeypatch.setattr(settings, "embedding_api_key", "fixture-key")
    monkeypatch.setattr(settings, "embedding_base_url", "https://embedding.invalid/v1")
    monkeypatch.setattr(settings, "embedding_model", "fixture-model")
    monkeypatch.setattr(settings, "embedding_dim", 2)
    monkeypatch.setattr(settings, "rag_hybrid_enabled", True)
    return path, spec


def change_profile(profile, monkeypatch, **changes):
    path, spec = profile
    value = {**spec, **changes}
    target = path.with_name(hashlib.sha256(json.dumps(value).encode()).hexdigest() + ".json")
    target.write_text(json.dumps(value))
    monkeypatch.setattr(settings, "embedding_profile", str(target))
    return value


def sdk(monkeypatch):
    sent = []

    def respond(request):
        texts = json.loads(request.content)["input"]
        sent.extend(texts)
        return httpx.Response(
            200,
            json={
                "object": "list",
                "model": "fixture-model",
                "usage": {"prompt_tokens": 1, "total_tokens": 1},
                "data": [
                    {
                        "object": "embedding",
                        "index": i,
                        "embedding": [1.0, 0.0] if "TAIL" in text else [0.0, 1.0],
                    }
                    for i, text in enumerate(texts)
                ],
            },
        )

    client = OpenAI(
        base_url="https://embedding.invalid/v1",
        api_key=settings.embedding_api_key,
        max_retries=0,
        http_client=httpx.Client(transport=httpx.MockTransport(respond)),
    )
    monkeypatch.setattr(vectors, "_get_client", lambda: client)
    return sent


def documents(text):
    return [
        {
            "type": "note",
            "id": 1,
            "title": "标题",
            "content": text,
            "revision": content_revision("标题", text),
        },
        {
            "type": "note",
            "id": 2,
            "title": "干扰",
            "content": "无关文字",
            "revision": content_revision("干扰", "无关文字"),
        },
    ]


def test_unicode_long_body_preserves_source_offsets_tail_and_complete_token_budget(profile):
    body = "博客Rust👩🏽‍💻é\n" * 190 + "TAIL"
    original = [
        {**c, "type": "note", "id": 1, "title": "标题"} for c in split("标题", "## 路径\n" + body)
    ]
    refined = inputs.vector_chunks(original)
    policy = inputs.active_profile()
    assert policy.count("abcdef") == 8  # Full text plus BOS/EOS, despite embedded truncation=4.
    assert policy.count("a") == 3  # Padding has also been disabled.
    assert len(refined) > len(original)
    covered = set()
    for chunk in refined:
        start, end = chunk["start"], chunk["end"]
        assert chunk["text"][chunk["prefix_chars"] :] == body[start:end]
        covered.update(range(start, end))
        assert policy.count(inputs.document_text("标题", chunk["text"], policy)) <= 64
        assert chunk["section_id"] == "s1" and chunk["section_path"] == ["路径"]
    assert covered == set(range(len(body)))
    assert any("TAIL" in c["text"] for c in refined)
    assert inputs.vector_chunks(refined) == refined


def test_production_retrieval_uses_refined_vectors_and_reuses_unchanged_inputs(
    profile, monkeypatch, tmp_path
):
    sent = sdk(monkeypatch)
    store = vectors.VectorStore(tmp_path / "cache")
    monkeypatch.setattr(vectors, "_store", store)
    source = "## 路径\n" + "长文本" * 120 + "TAIL"
    index = search.RagIndex(corpus_loader=lambda: documents(source), vector_warmer=lambda _: None)
    index.build()
    lexical_count = len(index._chunks)
    chunks = inputs.vector_chunks(index._chunks)
    result = store.update(chunks, vectors.space_from_settings())
    assert result["embedded"] > lexical_count
    assert all(t.startswith("D: ") for t in sent)
    assert all(inputs.active_profile().count(t) <= 64 for t in sent)
    count = len(sent)
    assert store.update(chunks, vectors.space_from_settings())["skipped"] == "aligned"
    assert len(sent) == count
    hits, reason, missing = index._vector_ranked("TAIL", 3)
    assert reason is None and missing == 0 and hits[0]["id"] == 1
    assert any(m["end"] > 350 for m in hits[0]["matches"])
    assert sent[-1] == "Q: TAIL" and len(index._chunks) == lexical_count

    def forbidden(chunks):
        pytest.fail("an unchanged corpus/space must not be refined again on each query")

    monkeypatch.setattr(search, "vector_chunks", forbidden)
    assert index._vector_ranked("TAIL", 3)[0][0]["id"] == 1

    old_texts = set(sent[:-1])
    updated = [{**c, "text": c["text"] + "改"} if c["id"] == 2 else c for c in chunks]
    before = len(sent)
    changed = store.update(updated, vectors.space_from_settings())
    assert changed["embedded"] == 1 and len(sent) == before + 1
    assert set(sent[before:]).isdisjoint(old_texts)


def test_long_queries_are_complete_distinct_and_over_budget_never_reach_sdk(
    profile, monkeypatch, tmp_path
):
    change_profile(profile, monkeypatch, max_tokens=1024)
    sent = sdk(monkeypatch)
    monkeypatch.setattr(vectors, "_store", vectors.VectorStore(tmp_path / "cache"))
    query = "中" * 600 + "TAIL"
    assert vectors.embed_query(query) is not None
    assert vectors.embed_query(query) is not None
    assert sent == ["Q: " + query]
    assert vectors.embed_query(query[:-4] + "OTHER") is not None
    assert len(sent) == 2 and sent[1].endswith("OTHER")
    assert vectors.embed_query("中" * 1020) is None  # Prefix + special tokens count as well.
    assert len(sent) == 2


@pytest.mark.parametrize(
    "changes",
    [
        {"query_prefix": "Different: "},
        {"document_prefix": "Different: "},
        {"max_tokens": 63},
        {"model_revision": "different-quantization"},
        {"pooling": "mean"},
        {"normalization": "none"},
        {"add_special_tokens": False},
    ],
)
def test_profile_changes_invalidate_both_live_view_and_content_cache(
    profile, monkeypatch, tmp_path, changes
):
    sent = sdk(monkeypatch)
    store = vectors.VectorStore(tmp_path / "cache")
    chunks = [{"type": "note", "id": 1, "title": "T", "section": "s", "text": "body"}]
    old = vectors.space_from_settings()
    assert store.update(chunks, old)["embedded"] == 1
    change_profile(profile, monkeypatch, **changes)
    current = vectors.space_from_settings()
    assert current != old and vectors.chunk_key(current, "same") != vectors.chunk_key(old, "same")
    assert store.view() is None
    assert store.update(chunks, current)["embedded"] == 1
    assert len(sent) == 2


@pytest.mark.parametrize("fault", ["missing", "hash", "schema", "no-room"])
def test_invalid_profiles_leave_lexical_retrieval_available_without_embedding_requests(
    profile, monkeypatch, tmp_path, fault
):
    sent = sdk(monkeypatch)
    path, spec = profile
    if fault == "missing":
        monkeypatch.setattr(settings, "embedding_profile", str(tmp_path / "absent.json"))
    elif fault == "hash":
        change_profile(profile, monkeypatch, tokenizer_sha256="a" * 64)
    elif fault == "schema":
        raw = b'{"version":"1.0", "model":{"type":"invalid"}}'
        (path.parent / "bad-tokenizer.json").write_bytes(raw)
        change_profile(
            profile,
            monkeypatch,
            tokenizer_file="bad-tokenizer.json",
            tokenizer_sha256=hashlib.sha256(raw).hexdigest(),
        )
    else:
        change_profile(profile, monkeypatch, max_tokens=2)
    monkeypatch.setattr(vectors, "_store", vectors.VectorStore(tmp_path / "cache"))
    index = search.RagIndex(corpus_loader=lambda: documents("needle"), vector_warmer=lambda _: None)
    index.build()
    assert index.search("needle")[0]["id"] == 1
    assert search.last_route()["reason"] == "embedding_profile_error"
    assert vectors.embed_query("needle") is None
    assert sent == []


def test_api_without_profile_retains_full_query_and_needs_no_tokenizer(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "embedding_profile", "")
    monkeypatch.setattr(settings, "embedding_api_key", "fixture-key")
    sent = sdk(monkeypatch)
    monkeypatch.setattr(vectors, "_store", vectors.VectorStore(tmp_path / "cache"))
    query = "API query " * 70 + "TAIL"
    assert vectors.embed_query(query) is not None and sent == [query]
    assert vectors.space_from_settings().profile == ""


def test_explicit_nfc_normalizes_embedding_only_and_partitions_space(profile, monkeypatch):
    original = inputs.active_profile()
    decomposed = "Cafe\u0301"
    assert inputs.document_text("Title", decomposed, original).endswith(decomposed)
    change_profile(profile, monkeypatch, unicode_normalization="NFC")
    canonical = inputs.active_profile()
    assert canonical.signature != original.signature
    assert inputs.document_text("Title", decomposed, canonical).endswith("Café")
    assert inputs.query_text(decomposed, canonical).endswith("Café")
    source = [{"type": "note", "id": 1, "title": "Title", "section": "s", "text": decomposed}]
    assert inputs.vector_chunks(source)[0]["text"] == decomposed


@pytest.mark.parametrize("mismatch", [False, True])
def test_local_probe_normalizes_then_rejects_tokenizer_drift(profile, monkeypatch, mismatch):
    from rag import manage

    change_profile(profile, monkeypatch, unicode_normalization="NFC")
    policy = inputs.active_profile()
    seen = []

    def respond(request):
        value = json.loads(request.content)["content"]
        seen.append(value)
        ids = policy.tokenizer.encode(value).ids
        return httpx.Response(200, json={"tokens": [999] if mismatch else ids})

    client = httpx.Client(transport=httpx.MockTransport(respond))
    monkeypatch.setattr(manage.httpx, "Client", lambda **kw: client)
    monkeypatch.setattr(vectors, "_embed_texts", lambda texts, _: [[1, 0] for _ in texts])
    assert manage.probe(2, True)["ok"] is (not mismatch)
    assert any("é" in text for text in seen)
    assert all("e\u0301" not in text for text in seen)
