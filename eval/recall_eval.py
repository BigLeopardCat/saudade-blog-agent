#!/usr/bin/env python3
"""检索 eval：recall@k / MRR（文档级，直接测线上实现 rag/search.py）。

评测驱动原则：检索 eval 测的是线上检索代码（rag.search），不另写模拟实现。
queries 与 golden RAG 用例一一对应（eval/golden/basic.jsonl 的 rag_* 条目），
期望命中文档按出题意图标注（note/talk 的公开 id）。

用法：
  python3 eval/recall_eval.py                 # 跑线上检索，报告进 eval/report/runs/<ts>.json
  python3 eval/recall_eval.py --show          # 打印每 query 的 top-k 命中明细
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from rag.search import get_index, search  # noqa: E402

ROOT = Path(__file__).resolve().parent
REPORT_RUNS = ROOT / "report" / "runs"

# ── queries：与 golden 的 rag_* 用例一一对应（gold 出题意图）──
# expected 是公开 id（note 12/14/16/19，talk 23）；noise 样本 expected=[]，
# 检索命中仅作参考（命中可能属合理候选），诚实拒答判定走端到端 golden。
QUERIES: list[dict] = [
    {"id": "rag_git_branch",    "query": "Git 的分支为什么很轻量？",       "expected": ["note:16"]},
    {"id": "rag_git_svn",       "query": "Git 和 SVN 有什么区别？",        "expected": ["note:16"]},
    {"id": "rag_git_snapshot",  "query": "Git 的核心特点有哪些？",          "expected": ["note:16"]},
    {"id": "rag_ota_partition", "query": "ESP32-S3 OTA 更新需要哪些分区？", "expected": ["note:12"]},
    {"id": "rag_ota_http",      "query": "ESP32-S3 做 OTA 时遇到过什么问题？", "expected": ["note:12"]},
    {"id": "rag_fingerprint_pin", "query": "指纹模组有哪些引脚？",          "expected": ["note:14"]},
    {"id": "rag_fingerprint_crc", "query": "指纹模组的通信校验用的是什么算法？", "expected": ["note:14"]},
    {"id": "rag_arch_ports",    "query": "看板娘系统里 Python agent 跑在哪个端口？", "expected": ["note:19"]},
    {"id": "rag_arch_components", "query": "Python agent 用什么框架写的？", "expected": ["note:19"]},
    {"id": "rag_arch_memory",   "query": "agent 的对话记忆存在哪里？",      "expected": ["note:19"]},
    {"id": "rag_arch_check",    "query": "agent 怎么防止模型假装调用了工具？", "expected": ["note:19"]},
    {"id": "rag_deep_recursion", "query": "agent 的工具循环上限（recursion_limit）是多少？", "expected": ["note:19"]},
    {"id": "rag_deep_timeout",  "query": "agent 流式回复的总时长硬上限是多少秒？", "expected": ["note:19"]},
    {"id": "rag_talk_rag",      "query": "留言板里有人聊过 RAG 的本质吗？", "expected": ["talk:23"]},
    {"id": "rag_noise_docker",  "query": "有没有 Docker 部署博客的教程？",  "expected": []},
    {"id": "rag_noise_rust",    "query": "Rust 的 async/await 是怎么工作的？", "expected": []},
    {"id": "rag_noise_mysql",   "query": "MySQL 慢查询怎么优化？",          "expected": []},
    {"id": "rag_noise_cake",    "query": "博客里有做巧克力蛋糕的文章吗？",  "expected": []},
    {"id": "rag_noise_project_files", "query": "博客前端项目根目录有哪些配置文件？", "expected": []},
    {"id": "rag_noise_python_copy", "query": "Python 深拷贝和浅拷贝有什么区别？", "expected": []},
    {"id": "rag_noise_python_is", "query": "Python 里 == 和 is 有什么区别？", "expected": []},
]


def evaluate(idx, show: bool) -> dict:
    results = []
    for q in QUERIES:
        hits = search(q["query"], top_k=5)
        hit_keys = [f"{h['type']}:{h['id']}" for h in hits]
        rank = next((i + 1 for i, h in enumerate(hit_keys) if h in q["expected"]), None)
        results.append({
            "id": q["id"], "expected": q["expected"],
            "hits": hit_keys, "rank": rank,
            "recall1": rank == 1, "recall3": rank is not None and rank <= 3,
            "recall5": rank is not None,
        })
        if show:
            print(f"  {q['id']:<24} exp={q['expected']} rank={rank} hits={hit_keys}")

    positive = [r for r in results if r["expected"]]
    mrr = sum(1.0 / r["rank"] for r in positive if r["rank"]) / len(positive) if positive else 0
    r1 = sum(r["recall1"] for r in positive) / len(positive) if positive else 0
    r3 = sum(r["recall3"] for r in positive) / len(positive) if positive else 0
    r5 = sum(r["recall5"] for r in positive) / len(positive) if positive else 0
    noise = [r for r in results if not r["expected"]]
    noise_hit = sum(1 for r in noise if r["hits"]) / len(noise) if noise else 0
    return {"baseline": "rag.search (lexical 2/3-gram BM25)", "n": len(results),
            "recall@1": round(r1, 4), "recall@3": round(r3, 4), "recall@5": round(r5, 4),
            "MRR": round(mrr, 4), "noise_hit_rate": round(noise_hit, 4), "results": results}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--show", action="store_true")
    args = ap.parse_args()

    idx = get_index()
    idx.build()
    docs = idx._docs
    print(f"语料：{len(docs)} 文档（{sum(1 for d in docs if d['type']=='note')} note / "
          f"{sum(1 for d in docs if d['type']=='talk')} talk / "
          f"{sum(1 for d in docs if d['type']=='board')} board / 其余 announcement）")

    rep = evaluate(idx, args.show)
    print(f"\n== {rep['baseline']} ==")
    print(f"  recall@1={rep['recall@1']:.2f} recall@3={rep['recall@3']:.2f} "
          f"recall@5={rep['recall@5']:.2f} MRR={rep['MRR']:.2f} noise_hit={rep['noise_hit_rate']:.2f}")

    ts = time.strftime("%Y%m%d-%H%M%S")
    REPORT_RUNS.mkdir(parents=True, exist_ok=True)
    payload = {"ts": ts, "corpus": "live-api", "queries": len(QUERIES), "runs": [rep]}
    (REPORT_RUNS / f"{ts}.json").write_text(json.dumps(payload, ensure_ascii=False, indent=1))
    print(f"\n报告: eval/report/runs/{ts}.json")


if __name__ == "__main__":
    main()
