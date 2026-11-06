#!/usr/bin/env python3
"""Evaluate production raw-query retrieval with a fixed top-5 candidate window.

Reports ranking, candidate supply and actual retrieval routes. The optional
holdout measures domain aliases separately from the fixed main query set.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import report_archive  # noqa: E402  同目录：留档文件名（秒级 ts 同秒撞车 → 见模块头注）

from rag.search import (  # noqa: E402
    expansion_enabled,
    get_index,
    last_route,
    search,
    set_expansion,
    synonyms,
    tokenize,
)

ROOT = Path(__file__).resolve().parent
REPORT_RUNS = ROOT / "report" / "runs"

QUERIES: list[dict] = [
    {"id": "rag_git_branch",    "query": "Git 的分支为什么很轻量？",       "expected": ["note:16"]},
    {"id": "rag_git_svn",       "query": "Git 和 SVN 有什么区别？",        "expected": ["note:16"]},
    {"id": "rag_git_snapshot",  "query": "Git 的核心特点有哪些？",          "expected": ["note:16"]},
    {"id": "rag_ota_partition", "query": "ESP32-S3 OTA 更新需要哪些分区？", "expected": ["note:12", "note:14"]},
    {"id": "rag_ota_http",      "query": "ESP32-S3 的 OTA 升级是怎么实现的？", "expected": ["note:12", "note:14", "note:22"]},
    {"id": "rag_fingerprint_pin", "query": "指纹模组有哪些引脚？",          "expected": []},
    {"id": "rag_fingerprint_crc", "query": "指纹模组的通信校验用的是什么算法？", "expected": []},
    {"id": "rag_arch_ports",    "query": "看板娘系统里 Python agent 跑在哪个端口？", "expected": ["note:19"]},
    {"id": "rag_arch_ports_real", "query": "博客架构 前后端端口 技术栈", "expected": ["note:19"],
     "known_fail": True},
    {"id": "rag_arch_components", "query": "Python agent 用什么框架写的？", "expected": ["note:19"]},
    {"id": "rag_arch_memory",   "query": "agent 的对话记忆存在哪里？",      "expected": ["note:19"]},
    {"id": "rag_arch_check",    "query": "agent 怎么防止模型假装调用了工具？", "expected": ["note:19"]},
    {"id": "rag_deep_recursion", "query": "agent 的工具循环上限（recursion_limit）是多少？", "expected": ["note:19"]},
    {"id": "rag_deep_timeout",  "query": "agent 流式回复的总时长硬上限是多少秒？", "expected": ["note:19"]},
    {"id": "rag_eval_system",   "query": "RAG测评体系怎么建立？",          "expected": ["note:19"]},
    {"id": "rag_noise_docker",  "query": "有没有 Docker 部署博客的教程？",  "expected": []},
    {"id": "rag_noise_rust",    "query": "Rust 的 async/await 是怎么工作的？", "expected": []},
    {"id": "rag_noise_mysql",   "query": "MySQL 慢查询怎么优化？",          "expected": []},
    {"id": "rag_noise_cake",    "query": "博客里有做巧克力蛋糕的文章吗？",  "expected": []},
    {"id": "rag_noise_project_files", "query": "博客前端项目根目录有哪些配置文件？", "expected": []},
    {"id": "rag_noise_python_copy", "query": "Python 深拷贝和浅拷贝有什么区别？", "expected": []},
    {"id": "rag_noise_python_is", "query": "Python 里 == 和 is 有什么区别？", "expected": []},
]


_HOLDOUT_NOTE = "留出集不入基线读数：它是扩展的判据，不是站点的检索分数。"

HOLDOUT: list[dict] = [
    {"id": "h_token_short", "query": "令牌怎么签发？",
     "expected": ["note:22", "note:19", "note:14"]},
    {"id": "h_token_long", "query": "接口鉴权用的令牌是怎么签发的？",
     "expected": ["note:22", "note:19", "note:14"]},
    {"id": "h_disconnect", "query": "断线了会怎样？", "expected": ["note:19"]},
    {"id": "h_ota_air", "query": "空中升级怎么做的？",
     "expected": ["note:12", "note:14", "note:22"]},
    {"id": "h_page_closed", "query": "页面关掉后还会继续跑吗？", "expected": ["note:19"],
     "known_fail": True},
    {"id": "h_rss", "query": "这个站支持 RSS 订阅吗？", "expected": []},
]


def expansion_diag(idx) -> dict:
    """词表 × 语料落点：每对词的 key / val **各有多少 gram 在语料里出现过**。

    这是每对词**存在的理由**的现场证据，也是两类病因的辨识依据：
    `0/1`（key 的词一个 gram 都不在语料里）⇒ 查询有效 token 掉到 0，检索返回**空**（B 类）；
    `1/5`（如「空中升级」，只有「升级」落了地）⇒ 还搜得到东西，只是搜不到点 ⇒ **弱收益**样本。
    别把这两类读成同一件事，也别指望第二类的指标会动。
    """
    vocabulary = idx.vocabulary()

    def land(t: str) -> tuple[int, int]:
        gs = list(dict.fromkeys(tokenize(t.lower())))
        return sum(1 for g in gs if g in vocabulary), len(gs)

    pairs = []
    for p in synonyms():
        k, v = land(p["key"]), land(p["val"])
        pairs.append({"key": p["key"], "val": p["val"],
                      "key_landing": f"{k[0]}/{k[1]}", "val_landing": f"{v[0]}/{v[1]}"})
    return {"enabled": expansion_enabled(), "pairs": pairs}


_BASELINE_LEXICAL = "rag.search (lexical 2/3-gram BM25, chunk 级文档聚合 + 固定 top-k)"
_BASELINE_HYBRID = "rag.search (hybrid: 2/3-gram BM25 + 向量 RRF 融合)"


def configured_dial() -> str:
    """本进程**内存里**的档位（不读 .env：那正是"文件里写的"与"进程里跑的"之分）。"""
    from config.settings import settings
    if not settings.rag_hybrid_enabled:
        return "lexical（RAG_HYBRID_ENABLED 未开）"
    from rag.vector_index import degraded_reason
    why = degraded_reason()
    return (f"hybrid（EMBEDDING_MODEL={settings.embedding_model}）" if why is None
            else f"lexical（开关开着但向量路不可用：{why}）")


def hybrid_switch_on() -> bool:
    """开关本身的取值（不看凭据/索引）。比 `configured_dial()` 的字符串稳——
    用字符串前缀判"开没开"会把"开着但凭据缺"当成没开，恰好漏掉要报警的那一格。"""
    from config.settings import settings
    return bool(settings.rag_hybrid_enabled)


def routes_text(modes: dict) -> str:
    """`{'hybrid': N}` → `"hybrid×N"`（报告里存 dict，打印要人读得懂）。"""
    return "、".join(f"{k}×{v}" for k, v in sorted(modes.items()))


def _div(num: float, den: int, digits: int = 4) -> float:
    """比率（分母为 0 ⇒ 0.0，与改造前逐字一致）。"""
    return round(num / den, digits) if den else 0.0


def _measure_one(q: dict) -> tuple[dict, str]:
    """Retrieve one fixed candidate window and its actual route."""
    hits = search(q["query"], top_k=5)
    mode = last_route()["mode"]
    hit_keys = [f"{h['type']}:{h['id']}" for h in hits]
    rank = next((i + 1 for i, h in enumerate(hit_keys) if h in q["expected"]), None)
    return {
        "id": q["id"], "expected": q["expected"],
        "hits": hit_keys, "rank": rank,
        "recall1": rank == 1, "recall3": rank is not None and rank <= 3,
        "recall5": rank is not None,
        "known_fail": bool(q.get("known_fail")),
    }, mode


def _aggregate(results: list[dict]) -> dict:
    """Aggregate relevance, reciprocal-rank and candidate-supply observations."""
    positive = [r for r in results if r["expected"]]
    noise = [r for r in results if not r["expected"]]
    npos = len(positive)
    return {
        "n": len(results),
        "recall@1": _div(sum(1 for r in positive if r["rank"] == 1), npos),
        "recall@3": _div(sum(1 for r in positive if r["rank"] and r["rank"] <= 3), npos),
        "recall@5": _div(sum(1 for r in positive if r["rank"] is not None), npos),
        "MRR": _div(sum(1.0 / r["rank"] for r in positive if r["rank"]), npos),
        "noise_hit_rate": _div(sum(1 for r in noise if r["hits"]), len(noise)),
        "mean_candidates": _div(sum(len(r["hits"]) for r in results), len(results), 2),
        "mean_candidates_positive": _div(sum(len(r["hits"]) for r in positive), npos, 2),
        "known_fail": [r["id"] for r in results if r.get("known_fail")],
    }


def evaluate(idx, show: bool, queries: list[dict] | None = None) -> dict:
    """Evaluate one fixed query set; report the holdout separately."""
    queries = QUERIES if queries is None else queries
    results: list[dict] = []
    modes: dict[str, int] = {}          # 实际路线计数（不是配置，是这批 query 真走过的路）
    for q in queries:
        row, mode = _measure_one(q)
        modes[mode] = modes.get(mode, 0) + 1
        results.append(row)
        if show:
            print(f"  {q['id']:<24} exp={q['expected']} rank={row['rank']} hits={row['hits']}")
    return {"baseline": (_BASELINE_HYBRID if modes.get("hybrid") == len(results)
                         else _BASELINE_LEXICAL),
            "routes": modes, **_aggregate(results), "results": results}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--show", action="store_true")
    ap.add_argument("--no-expansion", action="store_true",
                    help="关掉查询侧同义扩展再跑（A/B 的另一臂，与 --holdout 成对使用）")
    ap.add_argument("--holdout", action="store_true",
                    help="加跑留出集（访客口吻，专门覆盖同义扩展的词对）")
    args = ap.parse_args()

    if args.no_expansion:
        set_expansion(False)

    try:
        from corpus_check import presence_check
        corpus = presence_check()
    except Exception as e:
        print(f"[corpus] 在位性检查失败（{e}），跳过")
        corpus = {}

    idx = get_index()
    idx.build()
    docs = idx._docs
    print(f"语料：{len(docs)} 篇公开文章")
    dial = configured_dial()
    print(f"档位（配置）：{dial}")

    ediag = expansion_diag(idx)
    print("查询扩展：" + ("关（A/B 关臂）" if not ediag["enabled"] else "开")
          + "  " + "；".join(f"{p['key']}→{p['val']} 落点 {p['key_landing']}/{p['val_landing']}"
                             for p in ediag["pairs"]))

    rep = evaluate(idx, args.show)
    print(f"\n== {rep['baseline']} ==")
    _mismatch = hybrid_switch_on() and rep["routes"].get("hybrid", 0) < rep["n"]
    print(f"  档位（实测）：{routes_text(rep['routes'])}"
          + ("   ⚠️ 开关开着但并非每条都融合 ⇒ 先查接线（凭据/索引/对齐），别读指标"
             if _mismatch else ""))
    print(f"  recall@1={rep['recall@1']:.2f} recall@3={rep['recall@3']:.2f} "
          f"recall@5={rep['recall@5']:.2f} MRR={rep['MRR']:.2f} noise_hit={rep['noise_hit_rate']:.2f}")
    print(f"  平均候选={rep['mean_candidates']:.2f}（正例 {rep['mean_candidates_positive']:.2f}）")
    kf = [(r["id"], r["rank"]) for r in rep["results"] if r.get("known_fail")]
    if kf:
        print("  已知 FAIL（词法表征局限，非回归）：" + "、".join(f"{i} rank={k}" for i, k in kf))

    rep_h = None
    if args.holdout:
        rep_h = evaluate(idx, args.show, HOLDOUT)
        print(f"\n== 留出集 {rep_h['n']} 条（访客口吻，覆盖同义扩展的词对）==")
        print(f"  recall@1={rep_h['recall@1']:.2f} recall@3={rep_h['recall@3']:.2f} "
              f"recall@5={rep_h['recall@5']:.2f} MRR={rep_h['MRR']:.2f} "
              f"noise_hit={rep_h['noise_hit_rate']:.2f}")
        hkf = [(r["id"], r["rank"]) for r in rep_h["results"] if r.get("known_fail")]
        if hkf:
            print("  已知 miss（与扩展无关，如实留档）："
                  + "、".join(f"{i} rank={k}" for i, k in hkf))
        print(f"  ⚠️ {_HOLDOUT_NOTE}两臂要**同一个进程里各跑一次**"
              f"（本次是{'关' if args.no_expansion else '开'}臂），"
              f"换臂读**计数与名次**，别只读通过率。")
        if not args.no_expansion:
            print("   → 对照臂：eval/recall_eval.py --holdout --no-expansion --show")

    REPORT_RUNS.mkdir(parents=True, exist_ok=True)
    with report_archive.open_archive(REPORT_RUNS) as (out, f):
        ts = Path(out).stem
        payload = {"ts": ts, "corpus": corpus, "queries": len(QUERIES), "runs": [rep],
                   "expansion": ediag}
        if rep_h is not None:
            payload["holdout"] = rep_h
        json.dump(payload, f, ensure_ascii=False, indent=1)
    print(f"\n报告: {out}")


if __name__ == "__main__":
    main()
