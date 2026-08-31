#!/usr/bin/env python3
"""向量检索 POC（20260831，rag-design.md §9）：词法 BM25 vs 百炼 text-embedding-v4 vs RRF 三路对比。

设计定论：
- 复用 rag/search.py 的语料与词法路（评测即线上实现，不另写模拟）
- 向量路：chunk 级 embedding（text-embedding-v4, 1024 维, float）→ 余弦相似度
  → 文档级聚合取最高分 chunk——与 BM25 的"chunk 打分 → 文档最高分聚合"完全同构，
  两路只在打分维度上不同，指标对比公平
- RRF：k=60 标准常量，按两路各自排名融合（k 大对低排名宽容，适合召回场景）
- 纯 Python 点积（零 numpy 依赖：语料 ~60 chunk × 1024 维，单查询全库点积微秒级；
  生产化后同样够用，不必引入依赖）
- embedding 落盘缓存 eval/cache/vector_poc.json（文本 md5 为键，语料变化自动重算；
  首次跑 ~57 条文本 ≈ 6 批 API 调用，之后全部命中缓存）
- key/base_url 复用 agent 现有 QWEN_API_KEY / QWEN_BASE_URL（百炼 compatible-mode，
  与 qwen-3.8-flash 同一工作空间，无需新增配置）

用法：
  .venv/bin/python eval/recall_vector_poc.py [--show] [--refresh]
报告：eval/report/runs/<ts>_vector_poc.json（三路指标并排存档）
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from config import settings  # noqa: E402
from rag.search import get_index  # noqa: E402
from recall_eval import QUERIES  # noqa: E402  (同目录，评测集单一数据源)

ROOT = Path(__file__).resolve().parent
CACHE = ROOT / "cache" / "vector_poc.json"
REPORT_RUNS = ROOT / "report" / "runs"

EMBED_MODEL = "text-embedding-v4"
EMBED_DIM = 1024
BATCH = 10          # 百炼 text-embedding 单请求 input 上限 10 条
RRF_K = 60          # RRF 标准常量
TOP_N = 100         # RRF 融合时两路各自取的排名深度（语料只有几十，全量无碍）


def _hash(text: str) -> str:
    return hashlib.md5(text.encode("utf-8")).hexdigest()


def load_cache() -> dict[str, list[float]]:
    if CACHE.exists():
        try:
            return json.loads(CACHE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def save_cache(cache: dict[str, list[float]]) -> None:
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    CACHE.write_text(json.dumps(cache), encoding="utf-8")


def embed(client, texts: list[str], cache: dict[str, list[float]],
          refresh: bool) -> list[list[float]]:
    """带缓存 embedding；新增文本批量调用 API，命中缓存直接取。

    健壮性：批量响应条数 != 请求条数时（实测 265 条批次有丢数据），
    降级为逐条调用兜底——POC 阶段就不允许静默缺 embedding。
    """
    keys = [_hash(t) for t in texts]
    fresh: list[str] = [t for t, k in zip(texts, keys) if refresh or k not in cache]
    if fresh:
        from openai import OpenAI
        hits = sum(1 for k in keys if k in cache)
        out: list[list[float]] = []
        for i in range(0, len(fresh), BATCH):
            batch = fresh[i:i + BATCH]
            try:
                resp = client.embeddings.create(
                    model=EMBED_MODEL, input=batch, dimensions=EMBED_DIM,
                    encoding_format="float")
                got = [v.embedding for v in sorted(resp.data, key=lambda v: v.index)]
                if len(got) != len(batch):
                    raise ValueError(f"期望 {len(batch)} 条，API 返回 {len(got)} 条")
            except Exception as e:
                print(f"  批量 {len(batch)} 条失败（{e}），降级逐条调用")
                got = []
                for t in batch:
                    resp = client.embeddings.create(
                        model=EMBED_MODEL, input=[t], dimensions=EMBED_DIM,
                        encoding_format="float")
                    got.append(resp.data[0].embedding)
            out.extend(got)
        for t, v in zip(fresh, out):
            cache[_hash(t)] = v
        save_cache(cache)
        print(f"  embedding 新增 {len(fresh)} 条（缓存 {hits} 条命中）")
    return [cache[k] for k in keys]


def cos_sim(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(x * x for x in b) ** 0.5
    return dot / (na * nb) if na and nb else 0.0


def vec_search(query_vec: list[float], chunks: list[dict], chunk_vecs: list[list[float]],
               top_k: int) -> list[dict]:
    """向量路：chunk 余弦 → 文档级聚合取最高分（与 rag/search.py 的 BM25 同构）。"""
    by_doc: dict[tuple[str, str], dict] = {}
    for c, v in zip(chunks, chunk_vecs):
        s = cos_sim(query_vec, v)
        key = (c["type"], c["id"])
        agg = by_doc.get(key)
        if agg is None:
            by_doc[key] = {"type": c["type"], "id": c["id"], "title": c["title"],
                           "score": s, "sections": [c["section"]]}
        elif s > agg["score"]:
            agg["score"], agg["sections"] = s, [c["section"]]
        elif c["section"] not in agg["sections"]:
            agg["sections"].append(c["section"])
    ranked = sorted(by_doc.values(), key=lambda x: -x["score"])[:top_k]
    for r in ranked:
        r["score"] = round(r["score"], 4)
    return ranked


def rrf_fuse(lex: list[dict], vec: list[dict], top_k: int) -> list[dict]:
    """RRF 融合（k=60）：同文档两路排名各贡献 1/(k+rank)，降序取 top_k。"""
    score: dict[tuple[str, str], float] = {}
    info: dict[tuple[str, str], dict] = {}
    for ranked in (lex, vec):
        for rank, d in enumerate(ranked, 1):
            key = (d["type"], d["id"])
            score[key] = score.get(key, 0.0) + 1.0 / (RRF_K + rank)
            agg = info.get(key)
            if agg is None:
                info[key] = {"type": d["type"], "id": d["id"], "title": d["title"],
                             "sections": list(d.get("sections", []))}
            else:
                for s in d.get("sections", []):
                    if s not in agg["sections"]:
                        agg["sections"].append(s)
    ranked = [dict(info[k], score=score[k])
              for k in sorted(score, key=lambda k: -score[k])][:top_k]
    for r in ranked:
        r["score"] = round(r["score"], 4)
    return ranked


def evaluate(route: str, hits_fn, show: bool) -> dict:
    results = []
    for q in QUERIES:
        hit_keys = [f"{h['type']}:{h['id']}" for h in hits_fn(q["query"])]
        rank = next((i + 1 for i, h in enumerate(hit_keys) if h in q["expected"]), None)
        results.append({
            "id": q["id"], "expected": q["expected"], "hits": hit_keys, "rank": rank,
            "recall1": rank == 1, "recall3": rank is not None and rank <= 3,
            "recall5": rank is not None,
        })
        if show:
            print(f"  {q['id']:<24} exp={q['expected']} rank={rank} hits={hit_keys}")

    positive = [r for r in results if r["expected"]]
    n_pos = len(positive)
    mrr = sum(1.0 / r["rank"] for r in positive if r["rank"]) / n_pos if n_pos else 0
    r1 = sum(r["recall1"] for r in positive) / n_pos if n_pos else 0
    r3 = sum(r["recall3"] for r in positive) / n_pos if n_pos else 0
    r5 = sum(r["recall5"] for r in positive) / n_pos if n_pos else 0
    noise = [r for r in results if not r["expected"]]
    noise_hit = sum(1 for r in noise if r["hits"]) / len(noise) if noise else 0
    return {"route": route, "n": len(results),
            "recall@1": round(r1, 4), "recall@3": round(r3, 4), "recall@5": round(r5, 4),
            "MRR": round(mrr, 4), "noise_hit_rate": round(noise_hit, 4)}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--show", action="store_true")
    ap.add_argument("--refresh", action="store_true", help="忽略缓存强制重嵌")
    args = ap.parse_args()

    idx = get_index()
    idx.build()
    chunks = idx._chunks
    docs = idx._docs
    print(f"语料：{len(docs)} 文档 / {len(chunks)} chunk（note {sum(1 for d in docs if d['type']=='note')} / "
          f"talk {sum(1 for d in docs if d['type']=='talk')} / board {sum(1 for d in docs if d['type']=='board')}）")

    # ── 词法路（线上实现，直接复用）──
    lex_by_query = {q["query"]: idx.search(q["query"], top_k=TOP_N) for q in QUERIES}

    # ── 向量路 ──
    from openai import OpenAI
    client = OpenAI(api_key=settings.active_llm_api_key,
                    base_url=settings.active_llm_base_url, timeout=60)
    cache = load_cache()
    print(f"embedding：语料 {len(chunks)} chunk + query {len(QUERIES)}（{EMBED_MODEL}, {EMBED_DIM} 维）")
    t0 = time.time()
    chunk_texts = [f"{c['title']}\n{c['text']}" for c in chunks]
    chunk_vecs = embed(client, chunk_texts, cache, args.refresh)
    query_vecs = embed(client, [q["query"] for q in QUERIES], cache, args.refresh)
    print(f"  embedding 完成 {time.time() - t0:.1f}s")
    vec_by_query = {q["query"]: vec_search(v, chunks, chunk_vecs, TOP_N)
                    for q, v in zip(QUERIES, query_vecs)}

    # ── RRF 融合 ──
    rrf_by_query = {q["query"]: rrf_fuse(lex_by_query[q["query"]], vec_by_query[q["query"]], TOP_N)
                    for q in QUERIES}

    print("\n== 三路指标并排（21 条 query，与 recall_eval 同集）==")
    routes = {"lexical (BM25 2/3-gram)": lex_by_query,
              "vector (text-embedding-v4)": vec_by_query,
              "RRF (k=60)": rrf_by_query}
    reps = []
    for name, by_query in routes.items():
        rep = evaluate(name, lambda q, bq=by_query: bq[q], args.show)
        reps.append(rep)
        print(f"  {name:<28} recall@1={rep['recall@1']:.2f} recall@3={rep['recall@3']:.2f} "
              f"recall@5={rep['recall@5']:.2f} MRR={rep['MRR']:.2f} noise_hit={rep['noise_hit_rate']:.2f}")
        if name == "RRF (k=60)":
            print()  # 三路指标后留一行，明细在下面

    ts = time.strftime("%Y%m%d-%H%M%S")
    REPORT_RUNS.mkdir(parents=True, exist_ok=True)
    payload = {"ts": ts, "corpus": "live-api", "queries": len(QUERIES), "runs": reps}
    (REPORT_RUNS / f"{ts}_vector_poc.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=1))
    print(f"报告: eval/report/runs/{ts}_vector_poc.json")


if __name__ == "__main__":
    main()
