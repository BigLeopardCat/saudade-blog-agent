"""Bilingual retrieval outcomes, source coordinates and embedding independence."""

from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from _rag_store import memory_store, memory_vectors  # noqa: F401

from eval.offline_rag_eval import bm25_run, evaluate
from eval.rag_dataset import load
from rag.corpus import content_revision
from rag.search import RagIndex
from rag.tokenization import analyzed, policy_info, tokenize

FIXTURE = Path(__file__).resolve().parents[1] / "eval/fixtures/bilingual_retrieval"


def test_bilingual_technical_queries_rank_relevant_documents_and_reject_absent_topics():
    data = load(FIXTURE, "test")
    run, _ = bm25_run(data, 8)
    for qid, query in data["queries"].items():
        relevant = {did for did, grade in data["qrels"][qid].items() if grade > 0}
        if query["answerable"]:
            assert next(iter(run[qid]), None) in relevant, qid
            assert relevant <= set(list(run[qid])[:3]), qid
        else:
            assert not run[qid], qid
    assert set(run["cpp"]) == {"cn_cpp", "en_cpp"}
    assert set(run["csharp"]) == {"cn_csharp", "en_csharp"}
    result = evaluate(data, run, [1, 3])
    assert result["summary"]["all"]["R@3"]["value"] == 1


@pytest.mark.parametrize("title", ["How", "Why"])
def test_common_english_words_remain_searchable_as_actual_titles(title):
    document = {"type": "note", "id": 1, "title": title, "content": "An essay about curiosity."}
    index = RagIndex(corpus_loader=lambda: [document], vector_warmer=lambda _: None)
    assert index.build()
    assert [row["id"] for row in index._lexical_ranked(title, 1)] == [1]
    assert index._lexical_ranked(title + " photosynthesisxyz", 1) == []


def test_document_features_keep_frequency_and_technical_identities():
    features = Counter(tokenize("博客博客 C++ C# C .NET Node.js BGE-M3 bge_m3"))
    assert features["博客"] == 2
    assert features["c++"] == features["c#"] == features["c"] == 1
    assert features[".net"] == features["node.js"] == 1
    assert features["bge"] == features["m3"] == 2
    assert tokenize("猫") == ["猫"]


def test_normalized_features_retain_original_source_offsets():
    source = "👩🏽‍💻 前缀 ﬂow CAFÉ cafe\u0301 ＦａｓｔＡＰＩ transactions C++ Straße"
    spans = list(analyzed(source))
    expected = {"flow": "ﬂow", "cafe": "CAFÉ", "fastapi": "ＦａｓｔＡＰＩ",
                "transact": "transactions", "c++": "C++", "strass": "Straße"}
    for term, original in expected.items():
        assert any(feature == term and source[start:end] == original
                   for feature, start, end in spans)
    assert any(term == "cafe" and source[start:end] == "cafe\u0301"
               for term, start, end in spans)


def test_stemmed_normalized_body_matches_locate_the_original_excerpt():
    content = (("前段说明。\n" * 90)
               + "The ﬂow connects CAFÉ networks with transactions.\n" + "结束。" * 10)
    document = {"type": "note", "id": 1, "title": "阅读定位", "content": content,
                "revision": content_revision("阅读定位", content)}
    index = RagIndex(corpus_loader=lambda: [document], vector_warmer=lambda _: None)
    assert index.build()
    result = index._lexical_ranked("flow cafe transaction", 1)
    assert result and result[0]["id"] == 1
    excerpt = result[0]["matches"][0]["excerpt"]
    assert content[excerpt["offset"]:excerpt["end"]] == excerpt["text"]
    assert "The ﬂow connects CAFÉ networks with transactions." in excerpt["text"]
    assert "body" in result[0]["matches"][0]["matched_fields"]


def test_concurrent_english_stemming_is_independent_per_request_thread():
    phrases = ["connections transactions", "coroutines deployments", "C++ CAFÉ ﬂow"] * 8
    expected = [tokenize(phrase) for phrase in phrases]
    with ThreadPoolExecutor(max_workers=4) as executor:
        assert list(executor.map(tokenize, phrases)) == expected


def test_lexical_rebuild_reuses_vectors_and_reports_its_own_policy(monkeypatch, tmp_path):
    from config.settings import settings
    from rag import vector_index

    monkeypatch.setattr(settings, "embedding_profile", "")
    monkeypatch.setattr(settings, "embedding_model", "fixture")
    monkeypatch.setattr(settings, "embedding_dim", 2)
    space = vector_index.space_from_settings()
    calls = []

    def embed(texts, _):
        calls.extend(texts)
        return [[1, 0] for _ in texts]

    monkeypatch.setattr(vector_index, "_embed_texts", embed)
    store = memory_store(tmp_path)
    document = {"type": "note", "id": 1, "title": "Transactions", "content": "事务与 transactions",
                "revision": content_revision("Transactions", "事务与 transactions")}
    index = RagIndex(corpus_loader=lambda: [document],
                     vector_warmer=lambda chunks: store.update(chunks, space))
    assert index.build()
    fingerprint, count = store.repository.snapshot().fingerprint, len(calls)
    assert count > 0
    assert index.build()
    assert len(calls) == count
    assert store.repository.snapshot().fingerprint == fingerprint
    assert index.status()["lexical_backend"]["tokenizer"] == policy_info()
