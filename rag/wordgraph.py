"""文章向量空间图谱的查询侧（只读）。

产物由 ``scripts/build_word_graph.py`` 写到 ``data/word_graph/``：

===============  ==========================================================
index.json       词表 + build_id + 模型名 + 维度 + strip_top
vectors.f32      L2 归一化后的节点向量（count × dim，小端 float32 行主序）
mean.f32         训练集均值（dim）
dirs.f32         被剔除的主方向（strip_top × dim）——**可能是 0 字节**
===============  ==========================================================

**20260916e：弃权闸已拆除。** 曾用文章级 BM25 判"图里认不认得这句话"当闸
（词法零 API 成本），它比不判更糟：`物联网`/`单片机`/`IOT`/`嵌入式` 这类
**显然在域内**的查询被拦成空返回，而向量侧本来给的是 设备 / 遥测 / 传感器 /
esp32 一群正确节点（用户实测）；`物联网`+两个字变 `物联网设备` 就过了，脆得
没有道理。更根本的是闸的词典取自**展示层词表**（为画图挑的 341 个词），
画图选词一改、搜索边界跟着漂。

现在查询一律走向量，"能不能答"不再由一个词法部件替访客裁决；向量侧真失败
（`embed_failed` / 产物缺失等）前端照旧降级到本地匹配——降级判据回到
"服务是否可用"，不再有"服务说没找到"这一态。

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
                      words=list(words), vecs=vecs, mean=mean, dirs=dirs)
    logger.info("[wordgraph] 载入产物 %s：%d 词 × %d 维（剔除主方向 %d 个）",
                idx.get("build_id"), len(words), dim, len(dirs))
    return True


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

    reason 全部是**故障**语义（empty_query / artifact_missing / embed_failed /
    dim_mismatch），调用方一律按"服务不可用"处理 → 前端降级到本地关键词匹配。
    20260916e 拆掉弃权闸后不再有"结论式弃权"（`no_match`）：域外查询照样走
    embedding，返回的就是最近的那几个词——判断"相不相关"交回给访客，不由一个
    词法部件代判（理由见模块顶部）。
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
    logger.info("[wordgraph] q=%.40s → %d 词（top=%.40s %.4f）%dms",
                text, len(hits), hits[0]["w"] if hits else "-",
                hits[0]["s"] if hits else 0.0, ms)
    return {"ok": True, "build_id": _cache["build_id"], "words": hits, "ms": ms}
