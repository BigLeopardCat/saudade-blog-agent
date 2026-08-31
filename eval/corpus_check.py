#!/usr/bin/env python3
"""语料在位性检查 + 基线快照（20260831，问题记录 1.27 根治）。

背景：语料走 live API（不在 git），golden/recall 的 expected 是"隐式依赖"——
改语料（删文章/改写）就是改评测。1.27 事故：指纹文章改写为 OBC 文档后两条
golden FAIL + recall@1 1.00→0.86，表现像"模型退化"实为期望失配，归因花掉一晚上。
本模块让"语料变化 → 评测失败"可归因：

- 在位性：评测启动时校验 expected 命中文档（recall_eval.QUERIES 的 type:id）
  是否仍在线上语料——缺失 → WARN（期望过期，改 expected 而不是怀疑模型）；
  无缺失的失败才是模型退化信号。
- 基线快照：每次评测报告携带语料文档数/类型分布 + 期望集哈希（expected 集合
  的排序摘要，语料变化导致 expected 更新时哈希变化）——基线是"变更点快照"，
  变更点之间（语料/用例/代码无变更）的指标波动才是回归信号；跨变更不追求
  数字连续，靠快照对账。

操作纪律（20260831）：**动语料后必须跑一次 recall_eval 留档**——
  - 删/改写 expected 命中文档：在位性 WARN + hash 变化会暴露，失败可归因；
  - 只新增文档：expected 在位性不受影响、**hash 不变但检索排名可能被抢占**
    （recall@1 悄悄变），旧基线失去可比性却无信号——此时 recall_eval（秒级，
    不走 LLM）跑一轮存档即新检索基线；golden 全量（~25 分钟）不必动一次跑一次。
  架构修改后的 golden 失败，先对账最近一次 recall_eval 留档的 hash：一致 → 归因
  架构；不一致 → 先归因语料变更。

用法：被 eval/run_golden.py 与 eval/recall_eval.py 在启动时调用（打印 WARN +
报告落快照字段），也可独立运行：
  .venv/bin/python eval/corpus_check.py
"""
from __future__ import annotations

import hashlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from rag.search import get_index  # noqa: E402
from recall_eval import QUERIES  # noqa: E402  (同目录，评测集单一数据源)


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
    digest = hashlib.sha256("|".join(exp).encode("utf-8")).hexdigest()[:10]
    return {
        "corpus_total": len(docs),
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
