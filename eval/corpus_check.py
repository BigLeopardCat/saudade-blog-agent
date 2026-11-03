#!/usr/bin/env python3
"""语料在位性检查 + 基线快照。
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from recall_eval import QUERIES  # noqa: E402  (同目录，评测集单一数据源)

from rag.search import get_index  # noqa: E402


def expected_set() -> list[str]:
    """recall 评测集期望命中文档集合（"type:id" 字符串，去重排序，稳定摘要输入）。"""
    return sorted({e for q in QUERIES for e in q["expected"]})


def corpus_snapshot() -> dict:
    """当前线上语料快照：文档数/类型分布/期望集哈希。"""
    idx = get_index()
    idx.build()
    docs = idx._docs
    by_type: dict[str, int] = {}
    for d in docs:
        by_type[d["type"]] = by_type.get(d["type"], 0) + 1
    exp = expected_set()
    from config import settings
    from rag.chunks import INDEX_VERSION
    from rag.corpus import content_revision, corpus_revision
    from rag.lexical import backend_info
    from rag.search import BM25_B, BM25_K1
    from rag.vector_index import space_from_settings
    query_hash = hashlib.sha256(json.dumps(QUERIES, ensure_ascii=False,
                               sort_keys=True).encode()).hexdigest()
    digest = hashlib.sha256("|".join(exp).encode("utf-8")).hexdigest()[:10]
    return {
        "corpus_total": len(docs),
        "corpus_hash": corpus_revision(docs),
        "document_hashes": {f"{d['type']}:{d['id']}": content_revision(d["title"], d["content"])
                            for d in docs},
        "queries_hash": query_hash,
        "index_version": INDEX_VERSION,
        "retrieval_config": {"bm25_backend": backend_info(),
            "bm25_k1": BM25_K1, "bm25_b": BM25_B,
            "hybrid": settings.rag_hybrid_enabled,
            "rrf_k": settings.rrf_k,
            "embedding_space_hash": hashlib.sha256(json.dumps(
                space_from_settings().as_dict(), sort_keys=True).encode()).hexdigest()},
        "retrieval_code_hash": hashlib.sha256(b"".join(
            source.read_bytes()
            for source in sorted((Path(__file__).resolve().parents[1] / "rag").glob("*.py"))
        )).hexdigest(),
        "corpus_by_type": by_type,
        "expected": exp,
        "expected_count": len(exp),
        "expected_hash": digest,
    }


def presence_check(verbose: bool = True) -> dict:
    """在位性校验：expected 命中文档是否仍在语料。缺失 → WARN 列表。"""
    snap = corpus_snapshot()
    idx = get_index()
    corpus_keys = {f"{d['type']}:{d['id']}" for d in idx._docs}
    missing = [e for e in snap["expected"] if e not in corpus_keys]
    if verbose:
        print(f"[corpus] 语料 {snap['corpus_total']} 篇 {snap['corpus_by_type']} | "
              f"期望 {snap['expected_count']} 个（hash={snap['expected_hash']}）")
        for m in missing:
            print(f"[corpus] WARN 期望命中文档 {m} 不在当前语料——期望过期（语料被删/改写），"
                  f"请更新 recall_eval.QUERIES 的 expected；本次失败若由此引起不算模型退化")
    snap["missing"] = missing
    return snap


if __name__ == "__main__":
    presence_check(verbose=True)
