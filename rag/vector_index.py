"""向量索引：混合检索（BM25 + 向量 + RRF）的向量那一路。

20261005 用户第 1 条。词法路在 `rag/search.py`（自研 BM25，恒在）；本模块只管
"把 chunk 嵌成向量、存好、按需重建"，以及融合用的 `rrf_fuse` / `cos_sim` 纯函数。
**融合编排在 `rag/search.py::RagIndex.search()`**，不在这里——这里不 import
`rag.search`（它是本模块的调用方，反向 import 就是环）。

## 为什么是"自研 f32 文件"而不是向量库

生产 venv 只有 11 个依赖、`==` 钉死，且**本机 `.venv` 就是产线 venv**（不许
`uv sync`）。语料是几十~几百篇、chunk 数百条 × 1024 维 ⇒ 单查询全库点积毫秒级，
纯 Python 就够（`rag/wordgraph.py` 的 `_cosines` 是同一处境下的同一个选择）。
为此在这里引一个编译型依赖（numpy/faiss/chroma/sqlite-vec），代价远大于收益。
**什么时候该换**：chunk 数上万、或单查询点积超过 ~50ms。那时换的是本模块的
`VectorView.similar`，出口契约（`rrf_fuse` 的入参）不变。

## 增量：增量在哪、全量在哪（别把它读成"整库增量"）

| 环节 | 增量? | 说明 |
|---|---|---|
| HTTP 拉语料 / BM25 重建 | **全量** | 本来就 <100ms，且评测依赖它的确定性 |
| **embedding API 调用** | **增量** | 唯一花钱、唯一慢的一环 |
| cache / manifest 落盘 | **全量重写** | 数百 KB 的本地 I/O，行级 patch 不值 |
| 每 worker 的内存视图 | 每次刷新重建 | 读盘 ~ms |

"内容是否有变"的判据是**内容寻址**：`chunk_key = md5(模型 + 端点 + 维度 + 文本)`。
所以——

  · 改一节 ⇒ 只有那一节（以及被 markdown 重切挤动的相邻节）换 key ⇒ 只嵌它们；
  · 改标题 ⇒ **该篇全部 chunk 重嵌**（`title` 进 embedding 文本，这是刻意的：
    把 title 从 key 里拿掉能省一次重嵌，代价是静默复用"旧标题"的向量）；
  · 删文章 / 删章节 ⇒ 新 keys 里没有它 ⇒ **0 次 API**，只是 manifest 不再引用它，
    `update()` 顺手按代数把孤儿剪掉（**没有单独的维护命令**：一个没人执行的入口
    等于不存在——R2 那次的 `--keep 3` 就是写了没人跑）；
  · 换模型 / 换端点 / 换维度 ⇒ key 里三个字段都变 ⇒ 天然全部 miss（**"同一模型名
    在不同平台不是同一个向量空间"**：所以 `base_url` 也进 key）。

**没有 `--force` 重嵌**：缓存键已经含模型/端点/维度，换模型本来就全部 miss；
"强制重嵌"只会在同一个向量空间里重复花钱买一模一样的向量。

## 不变式（逐条校验，任一不过就当作"没有可用向量"，绝不返回错向量）

① `manifest.space` 与当前配置的**请求空间**逐字段相等（模型/端点/请求维度）。
   在**装载时**与**每次取用**（`view_for`/`view`）各核一次：装载是一次性的，而进程
   活得比配置久（settings 是模块级单例，改它不必重启进程）。
② `count == len(keys)`，每个 key 都在 cache 里、行号在 `cache.f32` 范围内；
③ `manifest.cache_dim == cache.dim`（**实际维度**只有一处事实源：cache 文件头）。

维度分两个概念，别混：`space.dim` = **请求维度**（0 = 不向 API 传 `dimensions`，
不同供应商支持度不一，传了可能 400），进缓存键；`cache.dim` = **实际拿到几维**，
只有一处事实源。嵌入返回的长度与 `cache.dim` 不一致时**宁可丢弃**（记 `missing`），
也绝不把两个维度的向量混进同一个文件——`zip` 会静默截断，那是"看不出来的错"。

## 原子写（唯一顺序，别改）

    cache.f32 → cache.json → **最后** manifest.json

manifest 是唯一的提交点；前两个文件只被追加/整体重写。崩在任何一步，读者见到的
都是"旧 manifest + 旧 cache（或超集）"，恒自洽。**绝不原地覆盖一个还被活跃
manifest 引用的二进制**——那是半成品的来源（旧 manifest 指向被覆盖过的新字节）。
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import logging
import os
import threading
import time
from array import array
from dataclasses import dataclass
from operator import mul
from pathlib import Path

from config import settings as _settings

logger = logging.getLogger(__name__)

SCHEMA = 1
SEP = "\x1f"          # 键的分隔符：不可能出现在文本里（\x1f = US，控制字符）
PRUNE_KEEP_GENS = 2   # 孤儿向量保留几代（给"误删后马上改回来 / 分页抖动"留缓冲）

MANIFEST = "manifest.json"
CACHE_JSON = "cache.json"
CACHE_F32 = "cache.f32"
LOCK = "update.lock"


# ══════════════════════════════════════════════════════════════════════════
#  空间（模型 + 端点 + 请求维度）与键
# ══════════════════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class Space:
    """向量空间：换任何一个字段 = 换一个空间 = 旧向量全部失效。"""

    model: str
    base_url: str   # 进键：同一模型名在不同平台不是同一个向量空间
    dim: int        # **请求**维度；0 = 不传 dimensions，以返回长度为准

    def as_dict(self) -> dict:
        return {"model": self.model, "base_url": self.base_url, "dim": self.dim}

    @staticmethod
    def from_dict(d: dict) -> "Space":
        return Space(str(d.get("model", "")), str(d.get("base_url", "")),
                     int(d.get("dim", 0) or 0))


def space_from_settings() -> Space:
    """当前配置出来的请求空间。**只读 `settings`，别处不许再读环境变量**。"""
    dim = int(getattr(_settings, "embedding_dim", 0) or 0)
    return Space(model=_settings.embedding_model.strip(),
                 base_url=_settings.embedding_base_url.strip(),
                 dim=dim if dim > 0 else 0)


def chunk_text(title: str, text: str) -> str:
    """chunk 的 embedding 文本。**与向量路建库时用的必须是同一个函数产出的同一个
    字符串**——两处各写一次 f-string，哪天一边改了分隔符，缓存就整片 miss（慢而
    不报错）或者更糟：复用到另一个字符串的向量。"""
    return f"{title}\n{text}"


def chunk_key(space: Space, text: str) -> str:
    """内容寻址键：同一份文本在同一空间里恒得同一个键，换空间必得不同键。"""
    raw = f"{space.model}{SEP}{space.base_url}{SEP}{space.dim}{SEP}{text}"
    return hashlib.md5(raw.encode("utf-8")).hexdigest()


def corpus_fingerprint(space: Space, chunks: list[dict]) -> str:
    """当前 chunk 列表的指纹（**顺序敏感**）。

    用途只有一个：判断盘上的向量是否正好对应"现在这批 chunk 与它们的顺序"
    （见 `VectorStore.view_for`）。顺序变而内容没变（服务端分页抖动）指纹会变——
    于是这一轮退词法、后台重排一次内存视图，**不会**重新调 API（key 没变）。
    """
    parts = [f"{c.get('type','')}{SEP}{c.get('id','')}{SEP}{c.get('section','')}"
             f"{SEP}{chunk_key(space, chunk_text(c.get('title',''), c.get('text','')))}"
             for c in chunks]
    return hashlib.sha1("\n".join(parts).encode("utf-8")).hexdigest()


# ══════════════════════════════════════════════════════════════════════════
#  纯函数：相似度与融合（离线套件直接测这两个）
# ══════════════════════════════════════════════════════════════════════════

def cos_sim(a, b) -> float:
    """余弦。**不假设 provider 返回单位向量**（wordgraph 敢用点积是因为它自己归一化了）。"""
    dot = sum(map(mul, a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(x * x for x in b) ** 0.5
    return dot / (na * nb) if na and nb else 0.0


def rrf_fuse(lex: list[dict], vec: list[dict], k: int, top_k: int) -> list[dict]:
    """RRF 融合（Reciprocal Rank Fusion）：同文档两路排名各贡献 1/(k+rank)。

    **只对有排名的两路做融合，不做任何截断**——每一路的剪枝在各自那一路里已经做完
    （词法路的相对断崖在 `rag/search.py`，向量路取 top-N）。这里再按 RRF 分数截一刀
    是错的：RRF 分数量级 ≈1/(k+1)≈0.016，照搬词法那个"低于 top1×0.25"的断崖等于
    永不生效（k=60 时最小项 1/70≈0.0143 > 0.25×0.0328），只会让参数看着还在、其实
    没接线。候选宽度由调用方的 `top_k` 收口，并用 `recall_eval` 的 `mean_candidates`
    盯着（供给端变宽要量，不能凭感觉）。

    同分不并列处理（分数是连续量、实际不会撞），排序稳定：等分时按"先出现在 lex，
    再出现在 vec"的顺序 —— 词法路是既有通路，平手时照顾它不是坏取向。
    """
    scores: dict[tuple, float] = {}
    info: dict[tuple, dict] = {}
    for ranked in (lex, vec):
        for rank, d in enumerate(ranked, 1):
            key = (d["type"], d["id"])
            scores[key] = scores.get(key, 0.0) + 1.0 / (k + rank)
            agg = info.get(key)
            if agg is None:
                info[key] = {"type": d["type"], "id": d["id"], "title": d["title"],
                             "sections": list(d.get("sections") or [])}
            else:
                for s in (d.get("sections") or []):
                    if s not in agg["sections"]:
                        agg["sections"].append(s)
    out = [dict(info[key], score=round(scores[key], 4))
           for key in sorted(scores, key=lambda x: -scores[x])]
    for r in out:
        r["sections"] = r["sections"][:2]
    return out[:max(1, top_k)]


# ══════════════════════════════════════════════════════════════════════════
#  embedding 客户端（OpenAI 兼容；供应商不进代码）
# ══════════════════════════════════════════════════════════════════════════

_client = None
_client_sig: tuple | None = None
_client_lock = threading.Lock()


def _get_client():
    """按当前配置懒建 OpenAI 兼容客户端；配置变了就换一个。

    不传 `encoding_format`（默认就是 float，而多一个参数就多一处聚合平台可能不认的
    面）；`dimensions` 只在显式配了 `EMBEDDING_DIM>0` 时才传。
    """
    global _client, _client_sig
    sig = (_settings.embedding_api_key, _settings.embedding_base_url,
           float(_settings.embedding_timeout))
    with _client_lock:
        if _client is None or _client_sig != sig:
            from openai import OpenAI
            kwargs: dict = {"api_key": sig[0], "timeout": sig[2]}
            if sig[1]:
                kwargs["base_url"] = sig[1]
            _client = OpenAI(**kwargs)
            _client_sig = sig
    return _client


def _embed_raw(texts: list[str], space: Space) -> list[list[float]]:
    """一次批量调用。条数不符抛 ValueError（由调用方降级逐条）。"""
    client = _get_client()
    kwargs: dict = {"model": space.model, "input": texts}
    if space.dim > 0:
        kwargs["dimensions"] = space.dim
    resp = client.embeddings.create(**kwargs)
    got = [v.embedding for v in sorted(resp.data, key=lambda v: v.index)]
    if len(got) != len(texts):
        # 实测（PoC 注释）：批次里会静默丢数据 —— 不许当成"只是少了点"
        raise ValueError(f"期望 {len(texts)} 条，API 返回 {len(got)} 条")
    return got


def _batches(n: int) -> int:
    """n 条文本按 `EMBEDDING_BATCH_SIZE` 分批要发几次请求（只用于日志/统计）。"""
    size = max(1, int(_settings.embedding_batch_size or 10))
    return (n + size - 1) // size if n > 0 else 0


def _embed_texts(texts: list[str], space: Space) -> list[list[float] | None]:
    """批量嵌入 + 逐条兜底。返回与入参等长的列表，逐条成功/失败（None）。

    批量失败或条数不符 ⇒ 整批降级逐条重来（PoC 的既有做法）。某一条单独也失败 ⇒
    那条记 None（**部分成功**），由调用方写进 `missing` 而不是把整次更新丢掉——
    语料那么大、为一条坏文本放弃其余几十条，是拿小事故换大事故。
    """
    out: list[list[float] | None] = []
    size = max(1, int(_settings.embedding_batch_size or 10))
    for i in range(0, len(texts), size):
        batch = texts[i:i + size]
        try:
            got = _embed_raw(batch, space)
        except Exception as exc:                     # noqa: BLE001
            logger.warning("embedding 批量 %d 条失败（%s），降级逐条调用",
                           len(batch), exc)
            got = []
            for t in batch:
                try:
                    got.append(_embed_raw([t], space)[0])
                except Exception as one:             # noqa: BLE001
                    logger.warning("embedding 单条失败（%s）", one)
                    got.append(None)
        out.extend(got)
    return out


# ══════════════════════════════════════════════════════════════════════════
#  盘上产物
# ══════════════════════════════════════════════════════════════════════════

class VectorView:
    """与一批 chunk **按序对齐**的向量视图（只读，构造后不再变）。

    `vectors[i]` 对应调用方 chunks[i]。点积用 `array('f')` + `map(mul)`：纯 Python
    但不逐元素建中间列表。
    """

    __slots__ = ("vectors", "dim")

    def __init__(self, vectors: list[array], dim: int) -> None:
        self.vectors = vectors
        self.dim = dim

    def __len__(self) -> int:
        return len(self.vectors)

    def similar(self, q: array) -> list[float]:
        return [cos_sim(q, v) for v in self.vectors]


def _write_atomic(path: Path, data: bytes) -> None:
    """同目录 tmp + fsync + os.replace。同目录是必须的（跨设备 rename 不原子）。"""
    tmp = path.with_name(f"{path.name}.tmp.{os.getpid()}")
    with open(tmp, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def _read_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except Exception as exc:                          # noqa: BLE001
        logger.warning("读 %s 失败（%s）——按没有向量处理", path.name, exc)
        return None


class VectorStore:
    """`data/rag_vectors/` 的读侧（`load`/`view_for`/`view`）与写侧（`update`）。

    进程内一把线程锁（同进程多线程）+ 盘上一把 flock（**跨 4 个 worker**——
    `rag/graph_build.py` 头注已实证：内存锁跨进程无效，而这里恰恰是 4 个 uvicorn
    worker 共用一份盘上产物）。
    """

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self._lock = threading.Lock()
        self._manifest: dict | None = None
        self._view: VectorView | None = None
        self._view_fp: str | None = None
        self._view_space: Space | None = None
        # 已装载的盘上版本签名（gen, fingerprint）。**两个都要比**：`gen` 是省一次
        # 物化的快路，`fingerprint` 才是"这批向量是不是这批 chunk 的"的判据——
        # 只比 gen 有个静默死角：整库被清掉重来（换维度那一路）会让 gen 从头计数，
        # 撞回同一个值，于是这个 worker 永远停在不匹配的旧视图上（且没有任何声音）。
        self._sig: tuple | None = None
        self._missing: list[str] = []
        self._last_error: str | None = None

    # ── 读 ────────────────────────────────────────────────────────────

    @property
    def manifest_path(self) -> Path:
        return self.root / MANIFEST

    def load(self) -> bool:
        """读盘（不联网）。返回"有一份可用的活跃向量"。

        每次调用都重读 manifest（几百字节）——若有别的 worker 刚更新过（`gen` 变了），
        就地换视图。这就是"别的 worker 更新完，本 worker 最多滞后一个刷新周期"
        的那一半优化：轮询成本接近零，就不必等 600s。
        """
        m = _read_json(self.manifest_path)
        with self._lock:
            if m is None:
                self._manifest, self._view, self._view_fp = None, None, None
                self._view_space = None
                self._sig, self._missing = None, []
                return False
            sig = (m.get("gen"), m.get("fingerprint"))
            if self._sig == sig:
                return self._view is not None
            try:
                self._install(m)
            except Exception as exc:                  # noqa: BLE001
                logger.warning("向量索引加载失败（%s）——按没有向量处理", exc)
                self._manifest, self._view, self._view_fp = None, None, None
                self._view_space = None
                self._sig, self._missing = None, []
                self._last_error = "no_vectors"
                return False
            return self._view is not None

    def _install(self, m: dict) -> None:
        """校验不变式 ①②③ 并物化视图。不满足就抛——调用方按"没有向量"收口。

        **不在这里物化整个 cache**：读侧只要活跃的那 `count` 行（几十~几百行）；
        全量素材由写侧 `update()` 自己去读（`_read_cache`）。
        """
        if int(m.get("schema", 0)) != SCHEMA:
            raise ValueError(f"manifest schema={m.get('schema')} 不是 {SCHEMA}")
        want = space_from_settings()
        got = Space.from_dict(m.get("space") or {})
        if got != want:                               # ① 请求空间必须逐字段相等
            raise ValueError(f"空间不符：盘上 {got}，当前 {want}")
        keys = list(m.get("keys") or [])
        if len(keys) != int(m.get("count", -1)):      # ②
            raise ValueError("count 与 keys 长度不符")
        cj = _read_json(self.root / CACHE_JSON)
        if cj is None or int(cj.get("schema", 0)) != SCHEMA:
            raise ValueError("cache.json 缺失或 schema 不符")
        dim = int(cj.get("dim", 0) or 0)
        if dim <= 0:
            raise ValueError("cache.dim 非法")
        if int(m.get("cache_dim", 0)) != dim:         # ③
            raise ValueError(f"cache_dim 不符：manifest={m.get('cache_dim')} cache={dim}")
        entries = list(cj.get("entries") or [])
        raw = (self.root / CACHE_F32).read_bytes()
        if len(raw) != len(entries) * dim * 4:        # 截断/半成品必须被拒
            raise ValueError(f"cache.f32 长度 {len(raw)} != {len(entries)}×{dim}×4")
        row_of = {str(k): i for i, (k, _g) in enumerate(entries)}
        # 物化：只切活跃的那些行（几百行 × 1024 维，微秒级）
        rows: list[array] = []
        for k in keys:
            i = row_of.get(k)
            if i is None:
                raise ValueError(f"键 {k[:8]}… 不在 cache 里")
            a = array("f")
            a.frombytes(raw[i * dim * 4:(i + 1) * dim * 4])
            rows.append(a)
        self._manifest = m
        self._view = VectorView(rows, dim)
        self._view_fp = str(m.get("fingerprint") or "")
        self._view_space = got
        self._sig = (m.get("gen"), self._view_fp)
        self._missing = list(m.get("missing") or [])
        self._last_error = None

    def view_for(self, fingerprint: str) -> VectorView | None:
        """盘上视图**恰好对应这批 chunk**（含顺序）时返回它，否则 None。

        绝不返回"上一次语料"的向量：`search()` 拿不到视图就退回纯词法，而不是拿旧
        向量去融合新 chunk——那会把分数算在错的对象上，且完全没有声音。
        """
        with self._lock:
            if self._view is None or self._view_fp != fingerprint:
                return None
            if self._view_space != space_from_settings():
                return None
            return self._view

    def view(self) -> VectorView | None:
        """当前视图（不要求指纹对齐；给 `/health` 与降级原因用）。

        **同样核空间**：不变式①在装载时核过一次就够了吗——不够。装载是一次性的，
        而这个进程活得比配置久（`settings` 是模块级单例，改它不需重启进程）。
        空间一变，盘上那份就是"另一个向量空间里的东西"，此时报"向量路正常"
        或者拿它去融合，都是静默错误。
        """
        with self._lock:
            if self._view is None or self._view_space != space_from_settings():
                return None
            return self._view

    def missing(self) -> list[str]:
        with self._lock:
            return list(self._missing)

    def last_error(self) -> str | None:
        with self._lock:
            return self._last_error

    # ── 写 ────────────────────────────────────────────────────────────

    def update(self, chunks: list[dict], space: Space) -> dict:
        """按需嵌入并落盘；返回本次统计（`fingerprint` 供调用方核对后再挂视图）。

        拿不到 flock（别的 worker 正在更新）⇒ 直接返回，一次 API 都不打：语料更新
        本来容忍一个刷新周期，抢着重嵌只是双份账单。
        """
        self.root.mkdir(parents=True, exist_ok=True)
        texts = [chunk_text(c.get("title", ""), c.get("text", "")) for c in chunks]
        keys = [chunk_key(space, t) for t in texts]
        fp = corpus_fingerprint(space, chunks)

        with _Flock(self.root / LOCK) as got:
            if not got:
                logger.debug("向量更新：另一个 worker 正在更新，本轮跳过")
                return {"skipped": "locked", "fingerprint": fp,
                        "embedded": 0, "reused": 0, "api_calls": 0}
            self.load()          # 先按盘上的现状装载一次（签名没变就是几百字节的早退）
            m = _read_json(self.manifest_path) or {}
            if (m.get("fingerprint") == fp and not m.get("missing")
                    and int(m.get("count", -1)) == len(set(keys))
                    and self.view_for(fp) is not None):
                # 盘上已经就是这批 chunk ⇒ 什么都不写。**不做这一步的后果**：每个
                # worker 每 REFRESH_TTL 都重写一遍文件、代数 +1，四个 worker 互相
                # 看着对方换视图，日志里刷满"新嵌 0 / 复用 60"这种零信息量的行。
                #
                # ⚠️ 最后那个 `view_for(fp) is not None` 不是多余的保险，它堵的是
                # **冻结式降级**：只看 manifest 的元数据的话，`cache.f32` 被截断
                # （或 cache.json 丢了）而 manifest 恰好还写着"已对齐"时，这里会一直
                # 说"无事可做"——每个 worker 的 load() 都失败 ⇒ 视图恒 None ⇒ 恒走
                # 纯词法，而后台**永远不去修它**（判据在断言"盘上是对的"，却没验证
                # 它真能读出来）。判据必须落在"能不能用"上，不是"看起来对不对"。
                #
                # `missing` 非空时**故意**不短路：上一轮没嵌成的那几条要能重试。
                return {"skipped": "aligned", "fingerprint": fp, "embedded": 0,
                        "reused": len(set(keys)), "api_calls": 0}
            return self._update_locked(texts, keys, space, fp)

    def _update_locked(self, texts, keys, space, fp) -> dict:
        """已经在 flock 里：算差集 → 只嵌缺的 → 原子落盘。

        `gen` 是**单调代数**（旧的最大代数 +1），**不是时间戳**：`PRUNE_KEEP_GENS`
        说的是"给最近 N 代留窗"，只有整数代数讲得通。
        """
        old_keys, old_dim, old_rows = self._read_cache()
        m0 = _read_json(self.manifest_path) or {}
        retry = set(m0.get("missing") or [])   # 上一轮嵌失败的键：占着零向量行，要重试
        gen = max([int(m0.get("gen") or 0)] + [int(g) for g in old_keys.values()] + [0]) + 1

        seen: set[str] = set()
        pending: list[tuple[str, str]] = []
        empty_keys: set[str] = set()
        reused = 0
        for key, text in zip(keys, texts):
            if key in seen:
                continue
            seen.add(key)
            if key in old_rows and key not in retry:
                reused += 1
                continue
            if not text.strip():
                # 空 chunk 不送去嵌（有的 provider 直接对空串报错），但**仍要占一行**：
                # 视图是"与 chunks 下标对齐"的，少一行 = 整批错位。给它零向量——
                # cos_sim 对零向量恒 0（不会凭空得分），其它一切照旧。
                empty_keys.add(key)
                continue
            pending.append((key, text))

        embedded: list[tuple[str, array]] = []
        failed: list[str] = []
        if pending:
            vecs = _embed_texts([t for _, t in pending], space)
            adopted = old_dim
            for (key, _t), v in zip(pending, vecs):
                if not v:
                    failed.append(key)
                    continue
                row = array("f", v)
                if adopted and len(row) != adopted:
                    if not embedded and old_dim:
                        # 供应商把输出维度改了 ⇒ 新行与旧行不能同处一个文件（`zip` 会
                        # 静默截断成"看着对的错分数"）。整库作废，本批按新维度重建。
                        logger.error("embedding 维度从 %d 变成 %d —— 旧库整库作废重建",
                                     old_dim, len(row))
                        self._last_error = "dim_changed"
                        self._wipe_cache()
                        old_keys, old_rows, old_dim = {}, {}, 0
                        adopted = len(row)
                    else:
                        logger.warning("向量长度 %d 与已采用的 %d 不符，丢弃该条",
                                       len(row), adopted)
                        failed.append(key)
                        continue
                elif not adopted:
                    adopted = len(row)
                embedded.append((key, row))

        dim = old_dim or (len(embedded[0][1]) if embedded else 0)
        if dim <= 0:
            # 一条都没嵌成、也没有旧库 ⇒ 不写半份产物，诚实报错
            self._last_error = "api_error" if pending else "no_vectors"
            return {"fingerprint": fp, "embedded": 0, "reused": reused,
                    "api_calls": _batches(len(pending)), "missing": len(failed), "ok": False}

        # 组装新缓存。**每个 chunk 都必须有一行**（哪怕零向量）——manifest 的 keys
        # 是"按下标对齐 chunks"的，少一行就是整批错位；而 `missing` 只用来标记
        # "零向量是因为这轮嵌失败"，下一轮照旧重试（空文本的那批不重试）。
        keep: dict[str, tuple[array, int]] = {}
        for key in seen:
            row = old_rows.get(key)
            if row is not None:
                keep[key] = (row, int(old_keys.get(key, 0)))
        for key, row in embedded:
            keep[key] = (row, gen)
        zeros: array | None = None
        for key in empty_keys | set(failed):
            if key not in keep:
                zeros = zeros if zeros is not None else array("f", [0.0] * dim)
                keep[key] = (zeros, gen)
        # 孤儿（本轮不再被引用的旧键）：还在保留代数窗口内的留着——给"误删后马上
        # 改回来""服务端分页抖动"留缓冲。剪枝**就在这一步**，没有单独的命令：
        # 一次没人执行的维护入口等于不存在（R2 那次 `--keep 3` 的教训）。
        for key, g in old_keys.items():
            if key not in keep and int(g) >= gen - PRUNE_KEEP_GENS:
                keep[key] = (old_rows[key], int(g))

        order = [(k, g) for k, (_r, g) in keep.items()]
        _write_atomic(self.root / CACHE_F32,
                      b"".join(r.tobytes() for _k, (r, _g) in keep.items()))
        _write_atomic(self.root / CACHE_JSON, json.dumps(
            {"schema": SCHEMA, "dim": dim, "entries": order},
            ensure_ascii=False).encode("utf-8"))
        active_keys = [k for k in keys if k in keep]
        manifest = {
            "schema": SCHEMA, "gen": gen, "space": space.as_dict(),
            "fingerprint": fp, "count": len(active_keys), "keys": active_keys,
            "cache_dim": dim, "missing": failed,
            "built_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "stats": {"embedded": len(embedded), "reused": reused,
                      "api_calls": _batches(len(pending)),
                      "pruned": len(old_keys) - sum(1 for k in keep if k in old_keys)},
        }
        _write_atomic(self.manifest_path,
                      json.dumps(manifest, ensure_ascii=False).encode("utf-8"))
        self._last_error = None
        logger.info("向量索引已更新：新嵌 %d / 复用 %d / API %d 批 / 维度 %d / 缺 %d",
                    len(embedded), reused, _batches(len(pending)), dim, len(failed))
        self.load()      # 刚写下去的 manifest 代数变了 ⇒ 签名不等 ⇒ 会真重读并换视图
        return {"fingerprint": fp, "embedded": len(embedded), "reused": reused,
                "api_calls": _batches(len(pending)), "missing": len(failed),
                "dim": dim, "ok": True}

    # ── 缓存读写 ──────────────────────────────────────────────────────

    def _read_cache(self) -> tuple[dict[str, int], int, dict[str, array]]:
        """读整套缓存（键→代数、维度、键→向量）。坏文件一律按空缓存处理。"""
        cj = _read_json(self.root / CACHE_JSON)
        if not cj or int(cj.get("schema", 0)) != SCHEMA:
            return {}, 0, {}
        dim = int(cj.get("dim", 0) or 0)
        entries = list(cj.get("entries") or [])
        try:
            raw = (self.root / CACHE_F32).read_bytes()
        except FileNotFoundError:
            return {}, 0, {}
        if dim <= 0 or len(raw) != len(entries) * dim * 4:
            logger.warning("向量缓存长度不符（%d != %d×%d×4），按空缓存重建",
                           len(raw), len(entries), dim)
            return {}, 0, {}
        gens: dict[str, int] = {}
        rows: dict[str, array] = {}
        for i, (k, g) in enumerate(entries):
            a = array("f")
            a.frombytes(raw[i * dim * 4:(i + 1) * dim * 4])
            gens[str(k)] = int(g)
            rows[str(k)] = a
        return gens, dim, rows

    def _wipe_cache(self) -> None:
        for name in (CACHE_F32, CACHE_JSON, MANIFEST):
            try:
                (self.root / name).unlink()
            except FileNotFoundError:
                pass


class _Flock:
    """跨进程互斥（LOCK_EX|LOCK_NB）。拿不到就返回 False——**不等待**：等一个几百
    KB 的重建毫无意义，而它正是 4 个 worker 同时启动时的常态。"""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.fd: int | None = None

    def __enter__(self) -> bool:
        try:
            self.fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o644)
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            if self.fd is not None:
                os.close(self.fd)
                self.fd = None
            return False

    def __exit__(self, *exc) -> None:
        if self.fd is not None:
            try:
                fcntl.flock(self.fd, fcntl.LOCK_UN)
            finally:
                os.close(self.fd)
                self.fd = None


# ══════════════════════════════════════════════════════════════════════════
#  进程级单例 + 查询侧
# ══════════════════════════════════════════════════════════════════════════

_store: VectorStore | None = None
_store_lock = threading.Lock()
_warm_lock = threading.Lock()
_query_cache: "dict[str, array]" = {}
_query_order: list[str] = []
_query_lock = threading.Lock()


def get_store() -> VectorStore:
    global _store
    with _store_lock:
        if _store is None:
            _store = VectorStore(_settings.rag_vector_dir)
        return _store


def is_enabled() -> bool:
    """开关 + 凭据。**判据只有这一个**（`rag_hybrid_enabled`），别处不许另读环境变量。"""
    return bool(_settings.rag_hybrid_enabled) and bool(_settings.embedding_configured)


def degraded_reason() -> str | None:
    """向量路为什么没在跑（None = 正常在用或本来就关着）。

    存在的理由：混合与纯词法的**出口文本一模一样**，下游（planner / eval / 人）
    从候选行里看不出走了哪条路。没有这个出口，"开关坏了"永远发现不了。
    """
    if not _settings.rag_hybrid_enabled:
        return None                     # 关着是设定，不是降级
    if not _settings.embedding_configured:
        return "missing_credentials"
    st = get_store()
    st.load()       # 读一次盘（几百字节，签名没变就早退）：不读的话，"盘上有一份好
                    # 索引、只是这个进程还没查过"会被报成 "warming"，与"真的还在建"
                    # 混成同一个答案——判据必须能区分这两件事
    if st.last_error():
        return st.last_error()
    if st.view() is None:
        return "warming"
    if st.missing():
        return "vector_missing"
    return None


def route_status() -> dict:
    """给 tool meta / `/health` dials 用的一行事实（不含任何 key/URL）。"""
    st = get_store()
    st.load()
    view = st.view()
    reason = degraded_reason()
    return {"enabled": bool(_settings.rag_hybrid_enabled),
            "active": bool(is_enabled() and view is not None),
            "reason": reason,
            "vectors": 0 if view is None else len(view),
            "missing": len(st.missing())}


def embed_query(query: str) -> array | None:
    """查询向量：内存 LRU + 超时。失败返回 None（调用方退回纯词法）。

    查询串**截断**（同 `wordgraph` 的做法）：访客可控的无界输入没有理由整段送出去。
    """
    q = (query or "").strip()[:512]
    if not q:
        return None
    with _query_lock:
        hit = _query_cache.get(q)
    if hit is not None:
        return hit
    space = space_from_settings()
    try:
        got = _embed_texts([q], space)[0]
    except Exception as exc:                          # noqa: BLE001
        logger.warning("查询向量嵌入失败（%s）——本轮退回纯词法", exc)
        return None
    if not got:
        return None
    vec = array("f", got)
    st = get_store()
    dim = (st.view().dim if st.view() is not None else 0)
    if dim and len(vec) != dim:
        logger.warning("查询向量维度 %d 与索引 %d 不符——本轮退回纯词法", len(vec), dim)
        return None
    cap = max(1, int(_settings.embedding_query_cache or 256))
    with _query_lock:
        _query_cache[q] = vec
        _query_order.append(q)
        while len(_query_order) > cap:
            _query_cache.pop(_query_order.pop(0), None)
    return vec


def warm_async(chunks: list[dict]) -> None:
    """后台把向量索引补到"这批 chunks"。并发调用只起一个线程；不阻塞调用方。

    在 `RagIndex.build()` 之后被踢一脚（build 本身**不联网**——它的"快且确定"是
    L1 评测依赖的性质，把网络调用塞进去会让 600s 懒刷新变成偶发 600s 卡顿）。
    """
    if not is_enabled():
        return
    if not _warm_lock.acquire(blocking=False):
        return

    def _run() -> None:
        try:
            get_store().update(chunks, space_from_settings())
        except Exception:                             # noqa: BLE001
            logger.warning("向量索引后台更新失败（下一轮刷新重试）", exc_info=True)
        finally:
            _warm_lock.release()

    threading.Thread(target=_run, name="rag-vector-warm", daemon=True).start()
