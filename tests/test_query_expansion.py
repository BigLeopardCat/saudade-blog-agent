"""Domain aliases preserve raw-query terms, frequency and unrelated rankings."""

from pathlib import Path

import pytest

from eval.rag_dataset import load
from rag import search

CASES = [
    ("测评", "测评怎么样？", 5),
    ("令牌", "令牌怎么样？", 1),
    ("断线", "断线怎么样？", 2),
    ("空中升级", "空中升级怎么样？", 3),
]
DOCUMENTS = [
    {"type": "note", "id": 1, "title": "设备接入", "content": "JWT 由平台签发。"},
    {"type": "note", "id": 2, "title": "连接恢复", "content": "客户端断连触发中断。"},
    {"type": "note", "id": 3, "title": "固件分区", "content": "OTA 使用两个应用分区。"},
    {"type": "note", "id": 4, "title": "Git 分支", "content": "分支是指针，commit 是快照。"},
    {"type": "note", "id": 5, "title": "质量验收", "content": "评测区分确定性契约与任务质量。"},
]


@pytest.fixture
def index(monkeypatch):
    monkeypatch.setattr(search, "_EXPANSION_ENABLED", True)
    selected = search.RagIndex(corpus_loader=lambda: DOCUMENTS, vector_warmer=lambda _: None)
    selected.build()
    return selected


def hits(index, query):
    return [row["id"] for row in index._lexical_ranked(query, 5)]


def test_aliases_have_explicit_rationale_and_development_examples():
    pairs = search.synonyms()
    assert pairs and {row["key"] for row in pairs} == {row[0] for row in CASES}
    assert len({row["key"] for row in pairs}) == len(pairs)
    data = load(Path(__file__).resolve().parents[1] / "eval/fixtures/blog_retrieval", "dev")
    queries = [row["text"] for row in data["queries"].values()]
    for row in pairs:
        assert row["key"] != row["val"] and row["why"].strip()
        assert len(row["key"]) >= 2 and len(row["val"]) >= 2
        assert any(row["key"] in query for query in queries)


@pytest.mark.parametrize("key,query,expected", CASES)
def test_aliases_supply_only_vocabulary_terms(index, monkeypatch, key, query, expected):
    search.set_expansion(False)
    assert not search.expansion_enabled()
    before = hits(index, query)
    search.set_expansion(True)
    assert search.expansion_enabled() and hits(index, query)[0] == expected
    assert hits(index, query) != before
    monkeypatch.setattr(search, "_QUERY_SYNONYMS", tuple(
        {**row, "val": "太空梯"} if row["key"] == key else row for row in search.synonyms()))
    assert hits(index, query) == before


@pytest.mark.parametrize("query", [
    "分支为什么很轻量？", "commit 是快照吗？", "令牌和 JWT 如何签发？",
])
def test_unrelated_terms_and_already_present_alias_do_not_gain_weight(index, query):
    search.set_expansion(False)
    before = hits(index, query)
    search.set_expansion(True)
    assert hits(index, query) == before
