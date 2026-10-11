#!/usr/bin/env python3
"""检索 eval：recall@k / MRR（文档级，直接测线上实现 rag/search.py）。

评测驱动原则：检索 eval 测的是线上检索代码（rag.search），不另写模拟实现。
queries 与 golden 的 `rag_*` 用例**同源出题，但不是逐条对应**（20261009 核对，差三条）：
`rag_arch_ports_real`（20260920 现场回流）与 `rag_eval_system`（1.26 事故回流）只在 L1、
不进 golden；`rag_talk_rag` 随 20260901 检索池净化退出 L1、留在 golden（端到端仍覆盖）。
期望命中文档按出题意图标注（note/talk 的公开 id）。

**每条 query 跑两臂（20261011）**：线上那一臂（断崖开）+ 断崖前那一臂（`apply_cliff=False`）。
报告里因此多了 `cliff` 一格（裁掉多少候选 / 有没有动 top-3 里的名次，后者必须为空，理由见
`rag/search.py` 模块头《断崖保底 top-K》）。这正是"截断吃掉了什么"的现场——20261010 那次
语料 +1 篇导致两条 query 的 gold 被断崖切掉，是靠人手工比对两轮报告才定位的；现在它每次
都在报告里，且 `eval/cliff_probe.py` 拿它当夜间的证书。

用法：
  python3 eval/recall_eval.py                 # 跑线上检索，报告进 eval/report/runs/<ts>.json
  python3 eval/recall_eval.py --show          # 打印每 query 的 top-k 命中明细
  python3 eval/recall_eval.py --holdout       # 加跑留出集（查询侧同义扩展的判据集，见 HOLDOUT）
  python3 eval/recall_eval.py --holdout --no-expansion   # 同一进程里的对照臂（两臂交替跑）
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
    cliff_config,
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


# ── 留出集（20261009）：查询侧同义扩展的**判据集** ──────────────────────
# 为什么不并进 QUERIES：上面那 22 条是**回归集**——它们出题时挑的是"站内确实写了的主题"，
# 而站点文档用的是站内自己的说法（JWT / 断连 / 评测）。访客嘴里是另一套（令牌 / 断线 / 测评），
# 这套词在 22 条里**一条都没出现**（20261009 实测）⇒ 拿主集量同义扩展，跑出来的小数
# 与「有没有加这对词」**完全无关**（扩表前后四个指标一字不差）。度量一个东西要先让它出现在题里。
#
# 所以这里按**访客口吻**另出一套：每条至少覆盖 `rag/search.py::_QUERY_SYNONYMS` 的一对词
# （机械闸门 `tests/test_query_expansion.py` 会断言"词表里每一对都在标注集里被真的用到"，
# 覆盖不住就红——防的正是"加了词、却没人拿它出过题"）。
#
# 期望值口径与主集一致（**全部正确答案文档**，不是"最该排第一的那篇"）：
#   note:22 IoT 设备接入指南（JWT 签发/MQTT）、note:19 架构文档（断连中断/鉴权）、
#   note:14 固件接入参考（JWT）、note:12 OTA 问题与解决记录。
# 20261009 两臂实测（`--holdout` / `--holdout --no-expansion --show`，同一天同语料）：
#   留出集整体      关臂 0.40/0.40/0.60 MRR 0.44 → 开臂 0.80/0.80/1.00 MRR 0.84
#   主集 22 条      **四个指标一字不差**（0.92/1.00/1.00/0.96、噪声 0.89、平均候选 3.36）
#   令牌怎么签发？   空 → note:22 —— 扩展救回来的是**零候选**（B 类：有效 token 掉到 0）
#   接口鉴权用的令牌…  note:22 由 rank 2 升到 rank 1（期望集里 note:14 两臂都是首中 ⇒
#                    **标量不动**，只有名次动了——这类改善只能逐条看 `--show`）
#   断线了会怎样？    只有一篇无关文档(note:46) → note:19 居首
#   空中升级怎么做的？ 两臂都是 rank 1（**指标不动**，只是把 note:12 拉进了 @3）
#   页面关掉后还会继续跑吗？ rank 5 / 5 —— **与扩展无关的已知 miss**（如实留档，别记在扩展账上）
#   这个站支持 RSS 订阅吗？ 两臂一字不差（负控：不含 key 词的查询不受影响）
_HOLDOUT_NOTE = "留出集不入基线读数：它是扩展的判据，不是站点的检索分数。"

HOLDOUT: list[dict] = [
    {"id": "h_token_short", "query": "令牌怎么签发？",
     "expected": ["note:22", "note:19", "note:14"]},
    {"id": "h_token_long", "query": "接口鉴权用的令牌是怎么签发的？",
     "expected": ["note:22", "note:19", "note:14"]},
    {"id": "h_disconnect", "query": "断线了会怎样？", "expected": ["note:19"]},
    {"id": "h_ota_air", "query": "空中升级怎么做的？",
     "expected": ["note:12", "note:14", "note:22"]},
    # 与扩展无关的两条**负控**：不含任何 key 词 ⇒ 两臂必须一字不差（证明扩展不是无差别生效）。
    # 前一条同时是一条如实留档的 miss（真答案 note:19 排在 5），别把它的 rank 当成扩展的锅。
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
    post = getattr(idx, "_postings", {})

    def land(t: str) -> tuple[int, int]:
        gs = list(dict.fromkeys(tokenize(t.lower())))
        return sum(1 for g in gs if g in post), len(gs)

    pairs = []
    for p in synonyms():
        k, v = land(p["key"]), land(p["val"])
        pairs.append({"key": p["key"], "val": p["val"],
                      "key_landing": f"{k[0]}/{k[1]}", "val_landing": f"{v[0]}/{v[1]}"})
    return {"enabled": expansion_enabled(), "pairs": pairs}


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


def _div(num: float, den: int, digits: int = 4) -> float:
    """比率（分母为 0 ⇒ 0.0，与改造前逐字一致）。"""
    return round(num / den, digits) if den else 0.0


def _measure_one(q: dict) -> tuple[dict, str]:
    """一条 query 跑**两臂**，返回 `(逐条读数, 实测路线)`。

    两臂同 query、同 top_k=5、同开关，**只差断崖一下** ⇒ 两臂之差只能由断崖造成。
    路线必须在线上那一臂**之后**、断崖前那一臂**之前**读——那一臂也会记一次 route。
    """
    hits = search(q["query"], top_k=5)
    mode = last_route()["mode"]
    hit_keys = [f"{h['type']}:{h['id']}" for h in hits]
    rank = next((i + 1 for i, h in enumerate(hit_keys) if h in q["expected"]), None)
    pre_keys = [f"{h['type']}:{h['id']}" for h in search(q["query"], top_k=5, apply_cliff=False)]
    pre_rank = next((i + 1 for i, h in enumerate(pre_keys) if h in q["expected"]), None)
    return {
        "id": q["id"], "expected": q["expected"],
        "hits": hit_keys, "rank": rank,
        "recall1": rank == 1, "recall3": rank is not None and rank <= 3,
        "recall5": rank is not None,
        # 断崖前那一臂：`cut` = 被断崖丢掉的候选（**这就是它的产出**，正常非空）；
        # `rank_precliff` 与 `rank` 之差才是"断崖动了名次"的证据，那个必须恒为空。
        "hits_precliff": pre_keys, "rank_precliff": pre_rank,
        "cut": [k for k in pre_keys if k not in hit_keys],
        "known_fail": bool(q.get("known_fail")),
    }, mode


def _rank_changed(results: list[dict]) -> list[dict]:
    """断崖**动了 top-3 里名次**的那几条（空 = 保底那条结构约束今天成立）。

    名次变化只可能落在**第 4 名以后**（保底那一段是原样保留的，断崖只删不排）：gold 原本
    排 4/5 名、被裁 ⇒ @5 从真变假是断崖**该有的**代价，不是回归；反过来，gold 出现在
    top-3 里而两臂名次不同，就是保底破了——那才是违例。
    """
    return [{"id": r["id"], "rank_precliff": r["rank_precliff"], "rank": r["rank"]}
            for r in results
            if r["rank_precliff"] != r["rank"]
            and ((r["rank_precliff"] or 99) <= 3 or (r["rank"] or 99) <= 3)]


def _aggregate(results: list[dict]) -> dict:
    """把逐条读数合成指标（含断崖那一格）。**纯函数**——`evaluate` 只负责跑与打印。

    断崖的成绩单分两个数报：裁掉多少候选（**它的产出**）与有没有动 top-3 里的名次
    （**它不许碰的东西**）。只报前者会把"截断很勤快"读成"截断很安全"。
    """
    positive = [r for r in results if r["expected"]]
    noise = [r for r in results if not r["expected"]]
    npos = len(positive)
    return {
        "n": len(results),
        "recall@1": _div(sum(1 for r in positive if r["rank"] == 1), npos),
        "recall@3": _div(sum(1 for r in positive if r["rank"] and r["rank"] <= 3), npos),
        "recall@5": _div(sum(1 for r in positive if r["rank"] is not None), npos),
        "MRR": _div(sum(1.0 / r["rank"] for r in positive if r["rank"]), npos),
        # 噪声样本没有 gold：这一格判的是"有没有命中任何一篇"（合理性参考，诚实拒答的
        # 真判据在端到端 golden）。
        "noise_hit_rate": _div(sum(1 for r in noise if r["hits"]), len(noise)),
        # 供给端指标（20260920 批次 d）：平均候选数 = planner 视野宽度，也是"候选驱动读"
        # 的上游（十来篇语料 × top_k=8 曾几乎倒回整个语料库）。
        "mean_candidates": _div(sum(len(r["hits"]) for r in results), len(results), 2),
        "mean_candidates_positive": _div(sum(len(r["hits"]) for r in positive), npos, 2),
        # 断崖前那一臂：只对**正例**算 recall（与线上臂同口径，否则两臂不可比）。
        # @5 单列是因为它是断崖**该动**的那一格（它裁的就是尾巴）——与"保底破了"分开读。
        "cliff": {
            **cliff_config(),
            "recall@1_precliff": _div(sum(1 for r in positive if r["rank_precliff"] == 1), npos),
            "recall@3_precliff": _div(sum(1 for r in positive if r["rank_precliff"]
                                         and r["rank_precliff"] <= 3), npos),
            "recall@5_precliff": _div(sum(1 for r in positive
                                         if r["rank_precliff"] is not None), npos),
            "candidates_cut": sum(len(r["cut"]) for r in results),
            "queries_cut": sum(1 for r in results if r["cut"]),
            "rank_changed": _rank_changed(results),
        },
        "known_fail": [r["id"] for r in results if r.get("known_fail")],
    }


def evaluate(idx, show: bool, queries: list[dict] | None = None) -> dict:
    """跑一套 query，返回指标。

    `queries` 显式传入是为了让留出集与主集**走同一套度量代码**（复制一份出来的那天起，
    两个数就再也对不上了），但**分别报告**——留出集不是基线的一部分，混进同一个小数里
    会让"站点的检索分数"这个口径悄悄换掉。

    **每条 query 跑两臂**（20261011）：线上那一臂（默认 cutoff）与**断崖前那一臂**
    （`apply_cliff=False`）。同 query、同 top_k、同开关，只差断崖一下 ⇒ 两臂之差**只能**
    由断崖造成，报告里因此可以直接读"截断吃掉了谁"。这不是又一次评测：它是把一条此前
    只活在**注释里的经验证书**（"α≤0.25 与不截断逐条一致"，见 `rag/search.py` 模块头）
    变成报告里的一等公民——那张证书当年是拿一次实跑签发的，此后没人复验过，而它失效的
    方式就是静默的（20261010 语料 +1 篇，两条 query 的 gold 被断崖切掉，四个指标里只有
    recall@1 动，没有任何判据会指认凶手）。
    保底 top-3 落地后这两臂**在 top-3 内必须逐条一致**；不一致就是那条结构约束破了。
    （@5 不在此列——断崖裁的就是尾巴，gold 原本排 4/5 名被裁是它**该有**的代价，单列报。）

    逐条测量在 `_measure_one`、指标合成在 `_aggregate`——本函数只负责跑与打印
    （两臂那一段让 `evaluate` 超了复杂度预算，拆出去也顺带让"两臂只差断崖一下"局部可读）。
    """
    queries = QUERIES if queries is None else queries
    results: list[dict] = []
    modes: dict[str, int] = {}          # 实测路线计数（不是配置，是这批 query 真走过的路）
    for q in queries:
        row, mode = _measure_one(q)
        modes[mode] = modes.get(mode, 0) + 1
        results.append(row)
        if show:
            print(f"  {q['id']:<24} exp={q['expected']} rank={row['rank']} hits={row['hits']}"
                  + (f"   ✂ 断崖前 {row['hits_precliff']}（rank {row['rank_precliff']}）"
                     if row["hits_precliff"] != row["hits"] else ""))
    # 档位名按**实测**取：混着跑（部分查询融合、部分降级）时不冒认 hybrid——
    # 那种报告最容易被读成"混合检索的效果"，实际是两条路的指标混在一起。
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

    # 查询侧扩展开关：内存里的模块级开关，**不是 .env 拨盘**（见 rag/search.py 的
    # _EXPANSION_ENABLED 注）——A/B 就是"同一份代码、同一批语料、同一个进程里拨这一下"。
    if args.no_expansion:
        set_expansion(False)

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

    ediag = expansion_diag(idx)
    print("查询扩展：" + ("关（A/B 关臂）" if not ediag["enabled"] else "开")
          + "  " + "；".join(f"{p['key']}→{p['val']} 落点 {p['key_landing']}/{p['val_landing']}"
                             for p in ediag["pairs"]))
    # 词表×语料落点：`0/1` = key 的 gram 一个都不在语料里（查询有效 token 掉到 0 ⇒ 返回空）。
    # 落点为 0 的词对是这张表的主力；`1/5` 那种（「空中升级」靠「升级」半落地）是弱收益样本。

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
    # 断崖那一格（20261011）：**同一个 query 跑两臂**，报告因此能直接读"截断吃掉了谁"。
    # 判据放在这里而不是只写进 JSON：这是每次跑都会被人看到的那两行。
    cf = rep["cliff"]
    print(f"  断崖 α={cf['ratio']} 保底 top-{cf['min_keep']}：裁掉 {cf['candidates_cut']} 条候选"
          f"（{cf['queries_cut']}/{rep['n']} 条 query 动过候选）")
    if cf["rank_changed"]:
        print("  ❌ 断崖改变了名次（保底之后这**不该发生** ⇒ 先看那条结构约束，别读通过率）："
              + "、".join(f"{c['id']} {c['rank_precliff']}→{c['rank']}" for c in cf["rank_changed"]))
    else:
        print(f"  ✅ 断崖未动任何一条名次（断崖前 recall@1={cf['recall@1_precliff']:.2f} "
              f"@3={cf['recall@3_precliff']:.2f}，与线上那一臂一致）")
    # 已知 FAIL 单列：它们进 recall@1 分子分母（数字不美化），但要在报告里点名，
    # 免得好好的 0.92 被读成"改动引入的退化"（见 QUERIES 里 known_fail 条目的注释）
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

    # 留档名走 report_archive（20261002）：与 golden 的两个跑法**同一个目录、同一份实现**
    # ——名字此前是秒级 ts（这里还多一个 `-` 的分隔符差异），同一秒的两份 report 会互相覆盖。
    REPORT_RUNS.mkdir(parents=True, exist_ok=True)
    with report_archive.open_archive(REPORT_RUNS) as (out, f):
        ts = Path(out).stem
        payload = {"ts": ts, "corpus": corpus, "queries": len(QUERIES), "runs": [rep],
                   "expansion": ediag}
        # 断崖那一格的明细在 `runs[0]["cliff"]`（含逐条 rank_changed）；顶层再放一个**结论**
        # ——读报告的人（含 `eval/cliff_probe.py` 那个探针）先看这一行，不必进逐条明细里翻。
        payload["cliff_ok"] = not rep["cliff"]["rank_changed"]
        if rep_h is not None:
            # 留出集另起一个键：`runs` 是"站点检索基线"，别把扩展的判据集混进同一个读数
            payload["holdout"] = rep_h
        json.dump(payload, f, ensure_ascii=False, indent=1)
    print(f"\n报告: {out}")


if __name__ == "__main__":
    main()
