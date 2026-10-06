"""文章向量空间图谱的查询侧（只读）。

产物由 ``scripts/build_word_graph.py`` 写到 ``data/word_graph/``：

===============  ==========================================================
index.json       词表 + build_id + **空间（模型 / 端点）** + 维度 + strip_top
vectors.f32      L2 归一化后的节点向量（count × dim，小端 float32 行主序）
mean.f32         训练集均值（dim）
dirs.f32         被剔除的主方向（strip_top × dim）——**可能是 0 字节**
===============  ==========================================================

**用哪个 embedding 模型/端点不在这里决定**（20261007）：`rag/embed_space.py` 解析
（配了 `EMBEDDING_*` 就用它、没配回落 `QWEN_*` + `text-embedding-v4`），与建图脚本
`scripts/build_word_graph.py` 读的是**同一条规则**。两侧解出的空间不一致时，查询向量
会落在另一片空间里 —— 症状同样是"搜 X 结果飞到一个视觉上离 X 很远的角落"，
**而且不报错**。所以产物里记着它自己的空间，`_load()` 拿它当场对一次：不符就

* 记**一条** WARNING（每个 `build_id` 只记一次，不刷日志），
* `query_words` 返回 `{"ok": false, "reason": "space_mismatch"}` —— 在**花掉那次
  embedding 调用之前**就返回，

调用方（Rust → 前端）照既有降级链路退回本地关键词匹配。**处置只有一句**：去后台
「站点设置 → 向量图谱」重建一次（新产物会带上新空间）。老产物没有 `base_url` 这一格
——那一格缺就不判（只在这一格上 fail-open；模型名那格老产物也有）。
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

# 查询串上限。与 Rust 侧的长度闸一致（防匿名刷 embedding 费用）
QUERY_MAX = 64
TOP_K_DEFAULT = 8
TOP_K_MAX = 20
# 查询是热路径：5s。**别换成 EMBEDDING_TIMEOUT（15s）**——Rust 的 `AGENT_TIMEOUT = 6s`
# 压在它后面（src/routes/graph.rs），改成 15s 等于让上游先超时、静默退化成本地匹配。
EMBED_TIMEOUT = 5.0

_lock = threading.Lock()
_client = None
_space = None                      # 进程内解析一次（settings 运行期不会变）
# 进程级缓存。uvicorn 起 4 个 worker，各自持有一份（1.4MB × 4，可接受）
_cache: dict = {
    "build_id": None, "dim": 0, "count": 0,
    "words": [], "vecs": None, "mean": None, "dirs": [],
    "mismatch": None,              # 产物空间与当场解析出的空间不符时的说明（否则 None）
}


def space_now():
    """当前进程该用的 embedding 空间（两端同一条规则，见 `rag/embed_space.py`）。"""
    global _space
    if _space is None:
        from config import settings
        from rag.embed_space import space_of
        _space = space_of(settings)
    return _space


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


def _space_mismatch(idx: dict) -> str | None:
    """产物记的空间与当场解析出的空间不符时的说明；相符/判不了 ⇒ None。

    只比**模型名**与**端点**：维度不比——产物记的是**实测**维度，而配置里的 `dim` 是
    **请求**维度（0 = 没传 dimensions）。真维度对不上时 `transform_query` 会返回 None
    （`dim_mismatch`），那里才是它的判据。
    """
    space = space_now()
    model = str(idx.get("model") or "").strip()
    if model and model != space.model:
        return f"产物是 {model} 建的，当前配置的是 {space.model}"
    base = str(idx.get("base_url") or "").strip().rstrip("/")
    # 老产物没有 base_url 这一格 ⇒ 这一格不判（只在这里 fail-open；模型名那格老产物也有）
    if base and base != space.base_url.rstrip("/"):
        return f"产物建立在 {base}，当前配置的是 {space.base_url}"
    return None


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

    mismatch = _space_mismatch(idx)
    if mismatch:
        # 每个 build_id 只记一次（_load 在 build_id 不变时早退，天然只打一遍）。
        # 这条 WARNING 是**明着降级**的告警：图谱检索会退回本地关键词匹配，
        # 处置是去后台「站点设置 → 向量图谱」重建一次。
        logger.warning("[wordgraph] 产物与当前的 embedding 空间不一致（%s）"
                       "——图谱检索将退回本地关键词匹配，重建一次即可", mismatch)

    with _lock:
        _cache.update(build_id=idx.get("build_id"), dim=dim, count=len(words),
                      words=list(words), vecs=vecs, mean=mean, dirs=dirs,
                      mismatch=mismatch)
    logger.info("[wordgraph] 载入产物 %s：%d 词 × %d 维（剔除主方向 %d 个，%s）",
                idx.get("build_id"), len(words), dim, len(dirs),
                f"空间 {idx.get('model')}" if not mismatch else f"空间不符：{mismatch}")
    return True


def status() -> dict:
    """给部署后自查用（不对外暴露成端点）。"""
    if not _load():
        return {"ok": False, "reason": "artifact_missing", "dir": str(GRAPH_DIR)}
    space = space_now()
    out = {"ok": True, "build_id": _cache["build_id"], "count": _cache["count"],
           "dim": _cache["dim"], "strip_top": len(_cache["dirs"]),
           "space": f"{space.source}:{space.model}"}
    if _cache["mismatch"]:
        out["space_mismatch"] = _cache["mismatch"]
    return out


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
    """查询串 → 向量。**用解析出来的 embedding 空间**（见 `rag/embed_space.py`）：
    配了 `EMBEDDING_*` 就用它，否则回落 `QWEN_*` + `text-embedding-v4`——与建图脚本
    同一条规则。刻意**不**跟着 `settings.active_llm_*` 走：对话 provider 可能没有
    embeddings 端点（deepseek 就没有），跟着它走会在切 provider 时哑掉。

    `dimensions` 与建图侧同口径：**只在显式配了 `EMBEDDING_DIM > 0` 时才传**
    （`rag/vector_index.py::_embed_raw` 也是这么做的）。
    """
    global _client
    space = space_now()
    from rag.embed_space import missing_config
    if (why := missing_config(space)):
        # 与建图脚本同一句话（`rag/embed_space.py::missing_config`）：同一件事在终端与
        # 日志里必须说成一件，而且要说清缺的是哪一格。
        logger.warning("[wordgraph] 没有可用的 embedding 配置（%s），无法做向量查询", why)
        return None
    try:
        if _client is None:
            from openai import OpenAI
            _client = OpenAI(api_key=space.api_key,
                             base_url=space.base_url, timeout=EMBED_TIMEOUT)
        kwargs: dict = {"model": space.model, "input": [text]}
        if space.dim > 0:
            kwargs["dimensions"] = space.dim
        resp = _client.embeddings.create(**kwargs)
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
        if _cache["mismatch"]:
            logger.info("[wordgraph] 空间不符（%s），跳过预热——查了也会被拒",
                        _cache["mismatch"])
            return
        t0 = time.perf_counter()
        ok = _embed_one("预热") is not None
        logger.info("[wordgraph] 预热%s（%s），%.0fms", "完成" if ok else "失败",
                    space_now().describe(), (time.perf_counter() - t0) * 1000)
    except Exception:
        logger.exception("[wordgraph] 预热异常（不影响主链路）")


def query_words(q: str, top_k: int = TOP_K_DEFAULT) -> dict:
    """查询串 → 图谱里最近的若干词。**绝不抛异常**：这是个可降级端点，
    调用方（Rust）拿到的 ok=false 就是「前端退回本地关键词匹配」的信号。

    返回 {ok, reason?, build_id, words:[{w,s}], ms}

    reason 全部是**故障**语义（empty_query / artifact_missing / embed_failed /
    dim_mismatch / space_mismatch），调用方一律按"服务不可用"处理 → 前端降级到本地
    关键词匹配。（`space_mismatch` = 产物不是当前 embedding 空间建的，见模块头注；
    它在**花掉那次 embedding 调用之前**返回。）
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
    if _cache["mismatch"]:
        # 先于 _embed_one：一次调用都别花，也别拿另一片空间的向量去查这张图
        return {"ok": False, "reason": "space_mismatch", "words": []}

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
