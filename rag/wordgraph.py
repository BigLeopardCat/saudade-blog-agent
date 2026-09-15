"""文章向量空间图谱的查询侧（只读）。

产物由 ``scripts/build_word_graph.py`` 写到 ``data/word_graph/``：

===============  ==========================================================
index.json       词表 + build_id + 模型名 + 维度 + strip_top
vectors.f32      L2 归一化后的节点向量（count × dim，小端 float32 行主序）
mean.f32         训练集均值（dim）
dirs.f32         被剔除的主方向（strip_top × dim）——**可能是 0 字节**
bm25.json        BM25 弃权闸索引（文章级 postings/dl）——缺失则闸停用
===============  ==========================================================

**弃权闸**（20260916d，见模块下半部分）：向量检索对任何输入都会返回 top-8，
实测域内/域外的 top1 分数**重叠**（0.363 / 0.364），没有可用的绝对阈值；词法
BM25 对"图里没有这句话"天然给 0 分，用它当闸。闸在 embedding 之前跑，
域外查询零 API 成本。

为什么不用 numpy：**生产 venv 里没有它**（当初刻意没装，见 CLAUDE.md §2）。
这里要做的只是「一个查询向量 × 333 个节点向量」的点积，用 ``array('f')`` 读裸
float32 + 纯 Python 点积足够快（实测 ~15ms），不值得为它给生产环境加一个
编译依赖。

⚠ 查询向量必须与服务端**同一套变换**（归一化 → 减均值 → 减去主方向 → 再归一化）。
不这样做的话，图谱按「处理后」的相似度连边、查询却按「原始」相似度找人，会出现
「搜 X 结果飞到一个视觉上离 X 很远的角落」——而且不会报错，只是感觉不对。
"""

from __future__ import annotations

import array
import json
import logging
import math
import os
import sys
import threading
import time
from pathlib import Path

logger = logging.getLogger(__name__)

GRAPH_DIR = Path(__file__).resolve().parent.parent / "data" / "word_graph"

EMBED_MODEL = "text-embedding-v4"
# 查询串上限。与 Rust 侧的长度闸一致（防匿名刷 embedding 费用）
QUERY_MAX = 64
TOP_K_DEFAULT = 8
TOP_K_MAX = 20
EMBED_TIMEOUT = 5.0

_lock = threading.Lock()
_client = None
# 进程级缓存。uvicorn 起 2 个 worker，各自持有一份（1.4MB × 2，可接受）
_cache: dict = {
    "build_id": None, "dim": 0, "count": 0,
    "words": [], "vecs": None, "mean": None, "dirs": [],
    "gate": None,          # BM25 弃权闸索引；None = 停用（fail-open，见 _load_gate）
}


# ---------------------------------------------------------------- 载入产物

def _read_f32(path: Path) -> array.array:
    """读裸 float32。文件不存在/长度为 0/被截断到半个 float 都当空处理——
    dirs.f32 在 strip_top=0 时**本来就是 0 字节**，调用方必须容忍。"""
    a = array.array("f")
    try:
        size = os.path.getsize(path)
        if size < 4:
            return a
        with open(path, "rb") as f:
            a.fromfile(f, size // 4)
    except (OSError, EOFError):
        logger.warning("[wordgraph] 读不出 %s，按空处理", path.name)
        return array.array("f")
    if sys.byteorder != "little":
        a.byteswap()
    return a


def _load() -> bool:
    """加载产物；build_id 变了就整份热替换（重出图不必重启 agent）。"""
    try:
        idx = json.loads((GRAPH_DIR / "index.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False

    if idx.get("build_id") == _cache["build_id"]:
        return True

    dim = int(idx.get("dim") or 0)
    words = idx.get("words") or []
    if not dim or not words:
        return False
    vecs = _read_f32(GRAPH_DIR / "vectors.f32")
    if len(vecs) != len(words) * dim:
        logger.warning("[wordgraph] vectors.f32 大小 %d ≠ %d×%d，产物不完整",
                       len(vecs), len(words), dim)
        return False

    mean = _read_f32(GRAPH_DIR / "mean.f32")
    if len(mean) != dim:
        mean = array.array("f", [0.0] * dim)      # 没均值就当没减过，聊胜于无

    strip = int(idx.get("strip_top") or 0)
    flat = _read_f32(GRAPH_DIR / "dirs.f32")
    dirs = [flat[k * dim:(k + 1) * dim] for k in range(strip)]
    dirs = [d for d in dirs if len(d) == dim]

    with _lock:
        _cache.update(build_id=idx.get("build_id"), dim=dim, count=len(words),
                      words=list(words), vecs=vecs, mean=mean, dirs=dirs,
                      gate=_load_gate(idx.get("build_id"), words))
    logger.info("[wordgraph] 载入产物 %s：%d 词 × %d 维（剔除主方向 %d 个；弃权闸 %s）",
                idx.get("build_id"), len(words), dim, len(dirs),
                "开" if _cache.get("gate") else "**关（fail-open）**")
    return True


def _load_gate(build_id, words: list) -> dict | None:
    """读 BM25 弃权闸索引。**任何一处不对就返回 None = 闸停用**（fail-open）：
    这是个"宁可不拦也别拦错"的部件——闸误伤真查询（把有结果的查询判成没结果）
    比闸缺席严重得多，所以它只在自己完全自洽时才生效。"""
    try:
        g = json.loads((GRAPH_DIR / "bm25.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        logger.warning("[wordgraph] 没有 bm25.json，弃权闸停用（查询行为与加闸前一致）")
        return None
    if g.get("build_id") != build_id:
        logger.warning("[wordgraph] bm25.json 的 build_id %s ≠ index.json 的 %s，弃权闸停用",
                       g.get("build_id"), build_id)
        return None
    post = g.get("postings") or {}
    if set(words) - set(post):
        logger.warning("[wordgraph] 闸索引缺 %d 个词表词，弃权闸停用",
                       len(set(words) - set(post)))
        return None
    n_doc = int(g.get("n_doc") or 0)
    if n_doc <= 0 or len(g.get("docs") or []) != n_doc:
        logger.warning("[wordgraph] 闸索引的文档集不完整，弃权闸停用")
        return None
    # idf 现场还原：与 rag/search.py 同一条公式（那边 chunk 级，这边文章级）。
    # 只存 df 不存 idf —— 调公式时两个模块改一处。
    idf = {w: math.log(1.0 + (n_doc - len(v) + 0.5) / (len(v) + 0.5))
           for w, v in post.items()}
    return {"k1": float(g.get("k1") or 1.2), "b": float(g.get("b") or 0.75),
            "n_doc": n_doc, "avgdl": float(g.get("avgdl") or 1.0),
            "dl": [d["dl"] for d in g["docs"]], "postings": post, "idf": idf}


# ---------------------------------------------------------------- 弃权闸（词法）
# 为什么需要它：向量侧对**任何**输入都返回 top-8。实测（341 词产物，18 条探针）
# 域内查询 top1 落在 [0.363, 0.805]、域外（图里根本没有这些词）落在 [0.228, 0.364]
# —— 两带**重叠**，没有可用的绝对阈值。词法侧天然能给 0 分，于是用 BM25 判零当闸。
# 副作用是好的：闸在 embedding **之前**跑，域外查询一次 API 调用都不花。

def match_terms(q: str, vocab: dict[str, str]) -> list[str]:
    """查询串 → 图谱词表里的词（按出现顺序去重）。

    **不引分词器**（生产 venv 没有 jieba），改用"词表即词典"的最长匹配：
    中文段从**词表里最长的中文词**往下试到 2 字、ASCII 段整词 + 前缀容忍
    （短的那个 ≥3 字，防 "in" 命中一片）。规则与前端本地兜底路的 `locate.ts`
    **逐条对齐**——两条路对"认不认得这句话"必须给同一个答案，否则会出现
    "服务挂了反而找得到"的老毛病（20260916 大小写那次就是两条路判定不一致）。

    ⚠ 上界必须**从词表算**（`max_cjk`），不能写死 4。写死 4 的那版实测漏掉
    `兼容性问题`（5 字）——连它自己当查询都认不出自己。词表每次重建都会变长，
    这个洞会自己长回来。"""
    s = (q or "").lower()
    out: list[str] = []
    seen: set[str] = set()
    max_cjk = max((len(w) for w in vocab if w and not w.isascii()), default=2)

    def push(w: str) -> None:
        if w not in seen:
            seen.add(w)
            out.append(w)

    i = 0
    n = len(s)
    while i < n:
        c = s[i]
        if c.isascii() and (c.isalnum() or c == "_"):
            j = i
            while j < n and s[j].isascii() and (s[j].isalnum() or s[j] == "_"):
                j += 1
            tok = s[i:j]
            i = j
            if len(tok) < 2:
                continue
            if tok in vocab:
                push(vocab[tok])
                continue
            for k, w in vocab.items():
                if len(k) < 3 or len(tok) < 3:
                    continue
                if k.startswith(tok) or tok.startswith(k):
                    push(w)
            continue
        # 中文段：最长匹配（max_cjk → 2），命中即跳过命中长度，避免重叠命中
        matched = 0
        for length in range(max_cjk, 1, -1):
            if i + length > n:
                continue
            sub = s[i:i + length]
            if not all("一" <= ch <= "鿿" for ch in sub):
                continue
            if sub in vocab:
                push(vocab[sub])
                matched = length
                break
        i += matched or 1
    return out


def _bm25(terms: list[str], gate: dict) -> float:
    """文章级 BM25，取**最高分**那篇。terms 非空 ⇒ 分数必然 >0（闸索引只收
    存在于某篇文章的词），所以判零判的是"有没有词命中"，不是"分高分低"。"""
    n_doc = gate["n_doc"]
    avgdl = max(gate["avgdl"], 1e-9)
    k1, b = gate["k1"], gate["b"]
    scores = [0.0] * n_doc
    for t in terms:
        w = gate["idf"].get(t, 0.0)
        # .get 而非 []：_load_gate 已保证 postings ⊇ 词表 ⊇ terms，正常路径下不会缺；
        # 但 idf 那边用的是 .get，两处形状一致，免得以后换个调用方就 KeyError。
        for di, tf in gate["postings"].get(t, ()):
            dl = max(gate["dl"][di], 1)
            scores[di] += w * (tf * (k1 + 1.0)) / (tf + k1 * (1.0 - b + b * dl / avgdl))
    return max(scores) if scores else 0.0


def status() -> dict:
    """给部署后自查用（不对外暴露成端点）。"""
    if not _load():
        return {"ok": False, "reason": "artifact_missing", "dir": str(GRAPH_DIR)}
    return {"ok": True, "build_id": _cache["build_id"], "count": _cache["count"],
            "dim": _cache["dim"], "strip_top": len(_cache["dirs"])}


# ---------------------------------------------------------------- 查询变换

def transform_query(vec: list[float], dim: int, mean: array.array,
                    dirs: list[array.array]) -> list[float] | None:
    """归一化 → 减均值 → 减去各主方向投影 → 再归一化。与建图脚本 write_artifacts
    的注释、project_3d 的 transform 一一对应；改一处必须改另一处。"""
    if len(vec) != dim:
        return None
    n = sum(float(x) * float(x) for x in vec) ** 0.5
    if n <= 1e-12:
        return None
    x = [float(v) / n for v in vec]
    for k in range(dim):
        x[k] -= float(mean[k])
    for d in dirs:
        proj = 0.0
        for k in range(dim):
            proj += x[k] * float(d[k])
        for k in range(dim):
            x[k] -= proj * float(d[k])
    n2 = sum(v * v for v in x) ** 0.5
    if n2 <= 1e-12:
        return None
    return [v / n2 for v in x]


def _cosines(q: list[float], vecs: array.array, count: int, dim: int) -> list[float]:
    """节点向量已 L2 归一化，所以余弦 = 点积。用 map+operator.mul 走 C 循环，
    比下标 for 快好几倍（333×1024 次乘加，纯下标要 40ms 上下）。"""
    from operator import mul
    out = []
    for i in range(count):
        row = vecs[i * dim:(i + 1) * dim]
        s = 0.0
        for v in map(mul, row, q):
            s += v
        out.append(s)
    return out


def _embed_one(text: str) -> list[float] | None:
    """查询串 → 向量。**显式用 qwen 的 key/base_url**：settings 的 active provider
    可能是 deepseek（没有 embeddings 端点），跟着 active 走会在切 provider 时哑掉。"""
    global _client
    from config import settings
    if not settings.qwen_api_key:
        logger.warning("[wordgraph] 没有 QWEN_API_KEY，无法做向量查询")
        return None
    try:
        if _client is None:
            from openai import OpenAI
            _client = OpenAI(api_key=settings.qwen_api_key,
                             base_url=settings.qwen_base_url, timeout=EMBED_TIMEOUT)
        resp = _client.embeddings.create(model=EMBED_MODEL, input=[text])
        return list(resp.data[0].embedding)
    except Exception:
        logger.exception("[wordgraph] embedding 调用失败")
        return None


# ---------------------------------------------------------------- 对外入口

def warm() -> None:
    """预热 embedding 客户端。首次调用要 DNS + TLS 握手 + 建连，实测 ~3s；
    稳态只有 ~100ms。不预热的话，**每次重启 agent 后的第一个查询都会慢到**
    被上游超时掐掉、静默退化成本地匹配——用户看到的就是"刚部署完那会儿定位不准"。
    在 lifespan 里后台线程调一次，代价是一次 embedding 调用。"""
    try:
        if not _load():
            logger.info("[wordgraph] 无产物，跳过预热")
            return
        t0 = time.perf_counter()
        ok = _embed_one("预热") is not None
        logger.info("[wordgraph] 预热%s，%.0fms", "完成" if ok else "失败",
                    (time.perf_counter() - t0) * 1000)
    except Exception:
        logger.exception("[wordgraph] 预热异常（不影响主链路）")


def query_words(q: str, top_k: int = TOP_K_DEFAULT) -> dict:
    """查询串 → 图谱里最近的若干词。**绝不抛异常**：这是个可降级端点，
    调用方（Rust）拿到的 ok=false 就是「前端退回本地关键词匹配」的信号。

    返回 {ok, reason?, build_id, words:[{w,s}], ms}

    reason 有两种截然不同的语义，**调用方必须分开处理**（20260916d 加闸时定的）：
      · `no_match` —— 弃权闸判定"图里没有这句话的任何词"。这是**结论**不是故障：
        前端应当如实显示"没找到相关的词"，**不要**退回本地兜底（本地兜底对零命中
        的输入还有一层字符 bigram 兜底，它保证"任何输入都有落点"——而那个落点
        正是这条闸要消灭的"一本正经的胡话"）。
      · 其他（empty_query / artifact_missing / embed_failed / dim_mismatch）
        —— 服务侧问题，前端照旧降级到本地关键词匹配。
    """
    t0 = time.perf_counter()
    text = (q or "").strip()[:QUERY_MAX]
    if not text:
        return {"ok": False, "reason": "empty_query", "words": []}
    if not _load():
        return {"ok": False, "reason": "artifact_missing", "words": []}

    dim = _cache["dim"]
    count = _cache["count"]
    words = _cache["words"]
    vecs = _cache["vecs"]

    # 弃权闸在最前面：图里根本不认这句话就直接如实说没有，既不给访客看
    # 一本正经的胡话（向量侧对任何输入都会返回 8 个近邻），也省掉一次 embedding。
    gate = _cache.get("gate")
    terms: list[str] = []
    if gate:
        vocab = {w.lower(): w for w in words}
        terms = match_terms(text, vocab)
        if not terms:
            ms = int((time.perf_counter() - t0) * 1000)
            logger.info("[wordgraph] q=%.40s → 弃权（词表里没有任何一个词出现在查询里）%dms",
                        text, ms)
            return {"ok": False, "reason": "no_match", "words": [], "ms": ms}

    raw = _embed_one(text)
    if raw is None:
        return {"ok": False, "reason": "embed_failed", "words": []}
    qv = transform_query(raw, dim, _cache["mean"], _cache["dirs"])
    if qv is None:
        return {"ok": False, "reason": "dim_mismatch", "words": []}

    sims = _cosines(qv, vecs, count, dim)
    k = max(1, min(int(top_k or TOP_K_DEFAULT), TOP_K_MAX, count))
    order = sorted(range(count), key=lambda i: sims[i], reverse=True)[:k]
    hits = [{"w": words[i], "s": round(sims[i], 4)} for i in order]
    ms = int((time.perf_counter() - t0) * 1000)
    # 闸的分数只进日志、不进判据（判据是"terms 非空"，见模块顶部说明）。
    # 留着是为了以后要调阈值时有实测分布可依，而不是拍一个数。
    gate_txt = (f" 闸 {_bm25(terms, gate):.2f}/{','.join(terms[:4])}"
                if gate and terms else " 闸 关")
    logger.info("[wordgraph] q=%.40s → %d 词（top=%.40s）%s %dms",
                text, len(hits), hits[0]["w"] if hits else "-", gate_txt, ms)
    return {"ok": True, "build_id": _cache["build_id"], "words": hits, "ms": ms}
