"""Fixed candidate windows preserve weak matches, raw queries and fallback evidence."""

import pytest

from config import settings
from rag import search, vector_index


@pytest.fixture
def index():
    documents = [
        {"type": "note", "id": 90, "title": "架构文档",
         "content": "## 架构总览\n" + "架构 端口 后端 前端 技术栈 部署 链路 " * 4},
        {"type": "note", "id": 91, "title": "OTA 问题与解决记录",
         "content": "## 端口\n平台侧 OTA 服务监听 3100 的端口。"},
        {"type": "note", "id": 92, "title": "部署运维笔记",
         "content": "## 服务\n后端跑在 127.0.0.1:3000 的端口上。"},
        {"type": "note", "id": 93, "title": "杂记 A", "content": "顺手记一下部署这件事。"},
        {"type": "note", "id": 94, "title": "杂记 B", "content": "链路有时候会断。"},
        {"type": "note", "id": 95, "title": "杂记 C", "content": "后端偶尔会重启。"},
    ]
    result = search.RagIndex(corpus_loader=lambda: documents, vector_warmer=lambda _: None)
    result.build()
    return result


def test_top_k_keeps_weak_matches_and_has_stable_ranked_prefixes(index):
    query = "架构 端口 后端 前端 技术栈 部署 链路"
    rows = index._lexical_ranked(query, 5)
    assert len(rows) == 5 and rows[0]["id"] == 90
    assert rows[0]["score"] > 4 * rows[-1]["score"]
    assert {91, 92} <= {row["id"] for row in rows}
    for k in (1, 3, 5):
        assert index._lexical_ranked(query, k) == rows[:k]
    for row in rows:
        assert row["document_revision"] and row["section_ids"] and row["matches"]
        assert all(match["end"] > match["offset"] for match in row["matches"])


@pytest.mark.parametrize("title", ["如何", "为什么", "怎么样"])
def test_question_words_are_searchable_article_text(title):
    index = search.RagIndex(
        corpus_loader=lambda: [{"type": "note", "id": 1, "title": title, "content": "作品正文"}],
        vector_warmer=lambda _: None,
    )
    index.build()
    assert [row["id"] for row in index._lexical_ranked(title, 1)] == [1]


def test_missing_vectors_preserve_the_complete_lexical_window(index, monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "rag_hybrid_enabled", True)
    monkeypatch.setattr(settings, "embedding_api_key", "fixture-key")
    monkeypatch.setattr(settings, "embedding_model", "fixture-model")
    monkeypatch.setattr(settings, "embedding_profile", "")
    monkeypatch.setattr(vector_index, "_store", vector_index.VectorStore(tmp_path))

    def forbidden(*args, **kwargs):
        pytest.fail("a missing vector view must not send a query embedding request")

    monkeypatch.setattr(vector_index, "_embed_texts", forbidden)
    query = "架构 端口 后端 前端 技术栈 部署 链路"
    lexical = index._lexical_ranked(query, 5)
    assert index.search(query, 5) == lexical
    assert search.last_route()["mode"] == "degraded"
    assert search.last_route()["reason"] == "warming"


def test_standard_offline_run_does_not_score_unavailable_as_empty(monkeypatch):
    from pathlib import Path

    from eval.offline_rag_eval import bm25_run
    from eval.rag_dataset import load

    data = load(Path(__file__).resolve().parents[1] / "eval/fixtures/offline_smoke", "test")
    monkeypatch.setattr(search.RagIndex, "_lexical_ranked", lambda *args: None)
    with pytest.raises(RuntimeError, match="offline index unavailable"):
        bm25_run(data, 5)
