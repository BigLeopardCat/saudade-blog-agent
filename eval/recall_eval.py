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
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import report_archive  # noqa: E402  同目录：留档文件名（秒级 ts 同秒撞车 → 见模块头注）
from rag.search import get_index, last_route, search  # noqa: E402

ROOT = Path(__file__).resolve().parent
REPORT_RUNS = ROOT / "report" / "runs"

# ── queries：与 golden 的 rag_* 用例一一对应（gold 出题意图）──
# expected 是公开 id（note 12/14/16/19，talk 23）；noise 样本 expected=[]，
# 检索命中仅作参考（命中可能属合理候选），诚实拒答判定走端到端 golden。
QUERIES: list[dict] = [
    {"id": "rag_git_branch",    "query": "Git 的分支为什么很轻量？",       "expected": ["note:16"]},
    {"id": "rag_git_svn",       "query": "Git 和 SVN 有什么区别？",        "expected": ["note:16"]},
    {"id": "rag_git_snapshot",  "query": "Git 的核心特点有哪些？",          "expected": ["note:16"]},
    # 20260905：note:12（OTA 问题与解决记录）现完整作答分区问题（分区表冲突/
    # partitions.csv/ota_0·ota_1），理当排第一——期望补 12（期望=全部正确答案文档）
    {"id": "rag_ota_partition", "query": "ESP32-S3 OTA 更新需要哪些分区？", "expected": ["note:12", "note:14"]},
    # 20260905 晚：note:12 回归语料后三篇答此泛问——12=本地 HTTP 上传式 OTA 实现记录
    # （整篇即实现），14=固件侧平台 OTA（§3 轮询/esp_https_ota/A·B），22=平台侧 OTA 规范
    # （OTA 固件管理小节含 esp_https_ota/A/B/轮询）。平台词只 lock 在 E2E golden
    # （模型读候选列表会挑平台篇，note:12 居首时 E2E 实证仍 PASS）——文档级期望
    # 如实收三篇，防单篇语义被检索判死。
    {"id": "rag_ota_http",      "query": "ESP32-S3 的 OTA 升级是怎么实现的？", "expected": ["note:12", "note:14", "note:22"]},
    # 20260831：指纹文章已改写为 ESP32-S3-OBC 文档，语料无指纹内容——转 noise（expected=[]）。
    {"id": "rag_fingerprint_pin", "query": "指纹模组有哪些引脚？",          "expected": []},
    {"id": "rag_fingerprint_crc", "query": "指纹模组的通信校验用的是什么算法？", "expected": []},
    {"id": "rag_arch_ports",    "query": "看板娘系统里 Python agent 跑在哪个端口？", "expected": ["note:19"]},
    # 20260920：用户现场真实 query 回流（失败用例回流惯例）。现场症状=planner 读 top-1
    # 后答"我暂时没法准确回答"——top-1 是《Git从入门到入土》，答案在《架构文档》。
    # 根因=词法检索的短语巧合 × 长度归一：《Git》的 .gitignore 小节标题「主流技术栈」给出
    # 技术栈/技术/术栈 三个 n-gram，而这几个在 10 篇语料里 df=1 ⇒ idf 最高；《架构文档》在
    # 架构/后端/端口 上 tf 全面占优却被 BM25 长度归一压住。**本条是已知 FAIL（rank=2），
    # 不是达标项**：20260920 实测三类改法（文档级 BM25 主分 / 覆盖率加权 / 查询 span 归一）
    # 榜首均不动，故供给端本批只做候选截断（相对断崖，见 rag/search.py 头注），
    # 修它要动检索表征（语义检索或结构感知索引：代码块/标题行不计入证据）。
    # 留在此处的价值=防止任何排序改动把《架构文档》进一步挤出候选（@3 仍须为真）。
    {"id": "rag_arch_ports_real", "query": "博客架构 前后端端口 技术栈", "expected": ["note:19"],
     "known_fail": True},
    {"id": "rag_arch_components", "query": "Python agent 用什么框架写的？", "expected": ["note:19"]},
    {"id": "rag_arch_memory",   "query": "agent 的对话记忆存在哪里？",      "expected": ["note:19"]},
    {"id": "rag_arch_check",    "query": "agent 怎么防止模型假装调用了工具？", "expected": ["note:19"]},
    {"id": "rag_deep_recursion", "query": "agent 的工具循环上限（recursion_limit）是多少？", "expected": ["note:19"]},
    {"id": "rag_deep_timeout",  "query": "agent 流式回复的总时长硬上限是多少秒？", "expected": ["note:19"]},
    # 20260831：1.26 事故真实 query 回流（"RAG测评体系怎么建立"检索稀释案例——
    # 通用词"建立/运行/使用"把架构文档稀释到 rank 5；进评测集后任何检索改动
    # 都必须验证此用例不被拉低，P0 失败用例回流第一单）
    {"id": "rag_eval_system",   "query": "RAG测评体系怎么建立？",          "expected": ["note:19"]},
    # 20260901：rag_talk_rag（留言板查询，期望 talk:23）随检索池净化移出评测——
    # 检索语料只收文章后 talk 不再可检索；"留言板/说说里有没有人聊过 X"是数据查询
    # 场景，走 list_guestbook/list_talks 数据工具（端到端行为仍由 golden rag_talk_rag 覆盖）。
    {"id": "rag_noise_docker",  "query": "有没有 Docker 部署博客的教程？",  "expected": []},
    {"id": "rag_noise_rust",    "query": "Rust 的 async/await 是怎么工作的？", "expected": []},
    {"id": "rag_noise_mysql",   "query": "MySQL 慢查询怎么优化？",          "expected": []},
    {"id": "rag_noise_cake",    "query": "博客里有做巧克力蛋糕的文章吗？",  "expected": []},
    {"id": "rag_noise_project_files", "query": "博客前端项目根目录有哪些配置文件？", "expected": []},
    {"id": "rag_noise_python_copy", "query": "Python 深拷贝和浅拷贝有什么区别？", "expected": []},
    {"id": "rag_noise_python_is", "query": "Python 里 == 和 is 有什么区别？", "expected": []},
]


# ── 本次生效档位（20261005）──────────────────────────────────────────
# 两臂 A/B（`RAG_HYBRID_ENABLED=0` vs `=1`，见 docs/rag-design.md §9）**唯一能回答
# "开关真开了没"的就是这两行**：两份报告的指标若不是同一档位跑出来的，那些小数点的
# 差异读不出任何东西。所以既报**配置档**（本进程内存里的开关与凭据——不重新解析
# `.env`：.env 改完不重启不生效，那正是本仓"push ≠ 生效"的同族坑），也报**实测档**
# （检索自己记下来的本轮路线，见 rag/search.py 的 last_route）。两者不一致本身就是
# 结论：配置写着 hybrid 而实测全 lexical ⇒ 先修接线，别去调参。
_BASELINE_LEXICAL = "rag.search (lexical 2/3-gram BM25, chunk 级文档聚合 + 相对断崖)"
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


def evaluate(idx, show: bool) -> dict:
    results = []
    modes: dict[str, int] = {}          # 实测路线计数（不是配置，是这批 query 真走过的路）
    for q in QUERIES:
        hits = search(q["query"], top_k=5)
        mode = last_route()["mode"]
        modes[mode] = modes.get(mode, 0) + 1
        hit_keys = [f"{h['type']}:{h['id']}" for h in hits]
        rank = next((i + 1 for i, h in enumerate(hit_keys) if h in q["expected"]), None)
        results.append({
            "id": q["id"], "expected": q["expected"],
            "hits": hit_keys, "rank": rank,
            "recall1": rank == 1, "recall3": rank is not None and rank <= 3,
            "recall5": rank is not None,
            "known_fail": bool(q.get("known_fail")),
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
    # 供给端指标（20260920 批次 d）：平均候选数=planner 视野宽度，也是"候选驱动读"
    # 浪费的上游（十篇语料 × top_k=8 曾几乎倒回整个语料库）
    mean_n = sum(len(r["hits"]) for r in results) / len(results) if results else 0
    mean_n_pos = sum(len(r["hits"]) for r in positive) / len(positive) if positive else 0
    # 档位名按**实测**取：混着跑（部分查询融合、部分降级）时不冒认 hybrid——
    # 那种报告最容易被读成"混合检索的效果"，实际是两条路的指标混在一起。
    return {"baseline": (_BASELINE_HYBRID if modes.get("hybrid") == len(results)
                         else _BASELINE_LEXICAL),
            "routes": modes, "n": len(results),
            "recall@1": round(r1, 4), "recall@3": round(r3, 4), "recall@5": round(r5, 4),
            "MRR": round(mrr, 4), "noise_hit_rate": round(noise_hit, 4),
            "mean_candidates": round(mean_n, 2), "mean_candidates_positive": round(mean_n_pos, 2),
            "known_fail": [r["id"] for r in results if r.get("known_fail")],
            "results": results}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--show", action="store_true")
    args = ap.parse_args()

    # 语料在位性检查 + 基线快照（20260831）：期望命中文档不在语料 → WARN（期望过期
    # ≠ 检索退化），报告带语料快照与期望集哈希（与 run_golden 同源，见 corpus_check.py）。
    try:
        from corpus_check import presence_check
        corpus = presence_check()
    except Exception as e:
        print(f"[corpus] 在位性检查失败（{e}），跳过")
        corpus = {}

    idx = get_index()
    idx.build()
    docs = idx._docs
    print(f"语料：{len(docs)} 文档（全部 note——20260901 检索池净化，talk/board/announcement 不再入池）")
    dial = configured_dial()
    print(f"档位（配置）：{dial}")

    rep = evaluate(idx, args.show)
    print(f"\n== {rep['baseline']} ==")
    # 开关开着、实测却不是每条都融合 ⇒ 这一跑**量不出混合检索的效果**，先修接线。
    # 这行是"两臂 A/B"的守门人：没有它，一次"开关没拨上"的词法跑会被当成混合臂存档。
    _mismatch = hybrid_switch_on() and rep["routes"].get("hybrid", 0) < rep["n"]
    print(f"  档位（实测）：{routes_text(rep['routes'])}"
          + ("   ⚠️ 开关开着但并非每条都融合 ⇒ 先查接线（凭据/索引/对齐），别读指标"
             if _mismatch else ""))
    print(f"  recall@1={rep['recall@1']:.2f} recall@3={rep['recall@3']:.2f} "
          f"recall@5={rep['recall@5']:.2f} MRR={rep['MRR']:.2f} noise_hit={rep['noise_hit_rate']:.2f}")
    print(f"  平均候选={rep['mean_candidates']:.2f}（正例 {rep['mean_candidates_positive']:.2f}）")
    # 已知 FAIL 单列：它们进 recall@1 分子分母（数字不美化），但要在报告里点名，
    # 免得好好的 0.92 被读成"改动引入的退化"（见 QUERIES 里 known_fail 条目的注释）
    kf = [(r["id"], r["rank"]) for r in rep["results"] if r.get("known_fail")]
    if kf:
        print("  已知 FAIL（词法表征局限，非回归）：" + "、".join(f"{i} rank={k}" for i, k in kf))

    # 留档名走 report_archive（20261002）：与 golden 的两个跑法**同一个目录、同一份实现**
    # ——名字此前是秒级 ts（这里还多一个 `-` 的分隔符差异），同一秒的两份 report 会互相覆盖。
    REPORT_RUNS.mkdir(parents=True, exist_ok=True)
    with report_archive.open_archive(REPORT_RUNS) as (out, f):
        ts = Path(out).stem
        payload = {"ts": ts, "corpus": corpus, "queries": len(QUERIES), "runs": [rep]}
        json.dump(payload, f, ensure_ascii=False, indent=1)
    print(f"\n报告: {out}")


if __name__ == "__main__":
    main()
