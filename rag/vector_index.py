"""Optional vector retrieval with content-addressed immutable cache generations.

The writer fsyncs generation files before atomically replacing manifest.json.
Readers verify file hashes and corpus/space alignment. Schema 1 caches are not
trusted or imported: the first schema 2 update rebuilds embeddings once.
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import threading
import time
from array import array
from dataclasses import dataclass, field
from pathlib import Path

from config import settings as _settings
from rag.chunks import strategy_signature
from rag.embedding_inputs import (
    InputProfileError,
    active_profile,
    document_text,
    query_text,
    validate_inputs,
    vector_chunks,
)
from rag.vector_files import (
    _Flock,
    _read_json,
    _write_atomic,
    generation_history,
    prune_generations,
    read_generation,
    write_generation,
)

logger = logging.getLogger(__name__)

SCHEMA = 2
SEP = "\x1f"          # Namespace/text field separator.
PRUNE_KEEP_GENS = 2   # Number of generations retained for unreferenced keys.

MANIFEST = "manifest.json"
CACHE_JSON = "cache.json"
CACHE_F32 = "cache.f32"
LOCK = "update.lock"


# ══════════════════════════════════════════════════════════════════════════
#  空间（模型 + 端点 + 请求维度）与键
# ══════════════════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class Space:
    """Embedding namespace; changing any field invalidates cached vectors."""

    model: str
    base_url: str   # 进键：同一模型名在不同平台不是同一个向量空间
    dim: int        # **请求**维度；0 = 不传 dimensions，以返回长度为准
    profile: str = ""  # 固定 tokenizer / 指令 / 预算 / 模型版本的签名
    chunking: str = field(default_factory=strategy_signature)

    def as_dict(self) -> dict:
        return {"model": self.model, "base_url": self.base_url, "dim": self.dim,
                "profile": self.profile, "chunking": self.chunking}

    @staticmethod
    def from_dict(d: dict) -> "Space":
        return Space(str(d.get("model", "")), str(d.get("base_url", "")),
                     int(d.get("dim", 0) or 0), str(d.get("profile", "")),
                     str(d.get("chunking", "")))


def space_from_settings() -> Space:
    """Build the embedding namespace from application settings."""
    dim = int(getattr(_settings, "embedding_dim", 0) or 0)
    profile = active_profile()
    return Space(model=_settings.embedding_model.strip(),
                 base_url=_settings.embedding_base_url.strip(),
                 dim=dim if dim > 0 else 0, profile=profile.signature if profile else "")


def chunk_text(title: str, text: str) -> str:
    """Use the same document text for embedding requests and content keys."""
    return document_text(title, text, None)


def chunk_key(space: Space, text: str) -> str:
    """Hash the complete embedding namespace and exact request text."""
    raw = f"{space.chunking}{SEP}{space.model}{SEP}{space.base_url}{SEP}{space.dim}{SEP}{text}"
    if space.profile:
        raw = f"{space.profile}{SEP}{raw}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def corpus_fingerprint(space: Space, chunks: list[dict]) -> str:
    """Hash the ordered chunk identities and embedding-text keys."""
    profile = active_profile()
    parts = [f"{c.get('type','')}{SEP}{c.get('id','')}{SEP}{c.get('section','')}"
             f"{SEP}{chunk_key(space, document_text(c.get('title',''), c.get('text',''), profile))}"
             for c in chunks]
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()


# ══════════════════════════════════════════════════════════════════════════
#  纯函数：相似度与融合（离线套件直接测这两个）
# ══════════════════════════════════════════════════════════════════════════

def cos_sim(a, b) -> float:
    """SciPy cosine, sharing the zero-vector and shape rules of the batch view."""
    return VectorView([array("f", b)], len(b)).similar(array("f", a))[0]


def rrf_fuse(lex: list[dict], vec: list[dict], k: int, top_k: int) -> list[dict]:
    """Official LangChain RRF ordering plus blog-specific evidence metadata."""
    from langchain_classic.retrievers.ensemble import EnsembleRetriever
    from langchain_core.documents import Document
    from langchain_core.runnables import RunnableLambda

    info: dict[tuple, dict] = {}
    ranked_documents = []
    for ranked in (lex, vec):
        documents = []
        for d in ranked:
            key = (d["type"], d["id"])
            documents.append(Document(page_content="", metadata={"identity": key}))
            agg = info.get(key)
            if agg is None:
                agg = info[key] = {**d, "sections": [], "section_ids": [], "matches": []}
                agg.pop("score", None)
            for location in ("sections", "section_ids", "matches"):
                for value in d.get(location) or []:
                    if value not in agg[location]:
                        agg[location].append(value)
        ranked_documents.append(documents)
    # Only reuse the maintained retriever's public fusion method, not a second runtime.
    def retriever_for(rows: list[Document]) -> RunnableLambda:
        def retrieve(_: str) -> list[Document]:
            return rows

        return RunnableLambda(retrieve)

    ensemble = EnsembleRetriever(
        retrievers=[retriever_for(rows) for rows in ranked_documents],
        weights=[0.5, 0.5], c=k, id_key="identity",
    )
    fused = ensemble.invoke("")
    out = []
    for rank, document in enumerate(fused[:max(1, top_k)], 1):
        row = dict(info[document.metadata["identity"]], rank=rank)
        row["sections"] = row["sections"][:2]
        row["section_ids"] = row["section_ids"][:3]
        row["matches"] = row["matches"][:3]
        out.append(row)
    return out


# ══════════════════════════════════════════════════════════════════════════
#  embedding 客户端（OpenAI 兼容；供应商不进代码）
# ══════════════════════════════════════════════════════════════════════════

_client = None
_client_sig: tuple | None = None
_client_lock = threading.Lock()


def _get_client():
    """Reuse an OpenAI-compatible client while credentials, endpoint and timeout match."""
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
    """Embed one batch, requiring exactly one result for each input index."""
    client = _get_client()
    kwargs: dict = {"model": space.model, "input": texts}
    if space.dim > 0:
        kwargs["dimensions"] = space.dim
    resp = client.embeddings.create(**kwargs)
    ordered = sorted(resp.data, key=lambda v: v.index)
    if [v.index for v in ordered] != list(range(len(texts))):
        raise ValueError("Invalid embedding response indexes")
    got = [v.embedding for v in ordered]
    if len(got) != len(texts):
        # Partial responses cannot be aligned with the input batch.
        raise ValueError(f"期望 {len(texts)} 条，API 返回 {len(got)} 条")
    return got


def _batches(n: int) -> int:
    """Count the configured batch requests needed for n inputs."""
    size = max(1, int(_settings.embedding_batch_size or 10))
    return (n + size - 1) // size if n > 0 else 0


def _embed_texts(texts: list[str], space: Space) -> list[list[float] | None]:
    """Embed batches with per-input fallback; preserve failed inputs as None."""
    validate_inputs(texts, space.profile)
    out: list[list[float] | None] = []
    size = max(1, int(_settings.embedding_batch_size or 10))
    for i in range(0, len(texts), size):
        batch = texts[i:i + size]
        try:
            got: list[list[float] | None] = [*_embed_raw(batch, space)]
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
        logger.debug("Embedding progress %d/%d", i + len(batch), len(texts))
    return out


# ══════════════════════════════════════════════════════════════════════════
#  盘上产物
# ══════════════════════════════════════════════════════════════════════════

class VectorView:
    """与一批 chunk **按序对齐**的向量视图（只读，构造后不再变）。

    `vectors[i]` 对应调用方 chunks[i]。SciPy computes cosine distances over an
    immutable float64 matrix without starting BLAS worker threads.
    """

    __slots__ = ("vectors", "dim", "_matrix")

    def __init__(self, vectors: list[array], dim: int) -> None:
        self.vectors = vectors
        self.dim = dim
        import numpy as np

        self._matrix = np.asarray(vectors, dtype=np.float64).reshape(len(vectors), dim)
        if not np.isfinite(self._matrix).all():
            raise ValueError("Non-finite document embedding")
        self._matrix.setflags(write=False)

    def __len__(self) -> int:
        return len(self.vectors)

    def similar(self, q: array) -> list[float]:
        import numpy as np
        from scipy.spatial.distance import cdist

        if len(q) != self.dim:
            raise ValueError("Embedding dimension mismatch")
        if not np.isfinite(q).all():
            raise ValueError("Non-finite query embedding")
        if not self.vectors:
            return []
        similarities = 1.0 - cdist([q], self._matrix, metric="cosine")[0]
        # Failed/empty chunks deliberately use zero rows, whose distance is NaN.
        return np.nan_to_num(similarities, nan=0.0).tolist()


class DimensionChanged(ValueError):
    """The provider changed dimensions; all active rows need re-embedding."""


def _materialize(pending, values, old_dim: int, requested_dim: int):
    embedded: list[tuple[str, array]] = []
    failed: list[str] = []
    adopted = old_dim
    for (key, _), value in zip(pending, values):
        if not value or not all(math.isfinite(x) for x in value) or not any(value):
            failed.append(key)
            continue
        row = array("f", value)
        if (not any(row) or not all(math.isfinite(x) for x in row)
                or (requested_dim and len(row) != requested_dim)):
            failed.append(key)
            continue
        if adopted and len(row) != adopted:
            if old_dim and not embedded:
                raise DimensionChanged()
            failed.append(key)
            continue
        adopted = adopted or len(row)
        embedded.append((key, row))
    return embedded, failed


class VectorStore:
    """Read immutable vector generations and publish updates under a cross-process flock."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self._lock = threading.Lock()
        self._manifest: dict | None = None
        self._view: VectorView | None = None
        self._view_fp: str | None = None
        self._view_space: Space | None = None
        # Compare both generation and corpus fingerprint when reusing a loaded view.
        self._sig: tuple | None = None
        self._missing: list[str] = []
        self._last_error: str | None = None

    # ── 读 ────────────────────────────────────────────────────────────

    @property
    def manifest_path(self) -> Path:
        return self.root / MANIFEST

    def load(self) -> bool:
        """Reload a changed manifest; reject invalid generations without network access."""
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
        """Validate namespace, row alignment and dimensions; materialize only active rows."""
        if int(m.get("schema", 0)) != SCHEMA:
            raise ValueError(f"manifest schema={m.get('schema')} 不是 {SCHEMA}")
        want = space_from_settings()
        got = Space.from_dict(m.get("space") or {})
        if got != want:                               # ① 请求空间必须逐字段相等
            raise ValueError(f"空间不符：盘上 {got}，当前 {want}")
        keys = list(m.get("keys") or [])
        if len(keys) != int(m.get("count", -1)):      # ②
            raise ValueError("count 与 keys 长度不符")
        cj, raw = read_generation(self.root, m)
        if cj is None or int(cj.get("schema", 0)) != SCHEMA:
            raise ValueError("cache.json 缺失或 schema 不符")
        dim = int(cj.get("dim", 0) or 0)
        if dim <= 0:
            raise ValueError("cache.dim 非法")
        if int(m.get("cache_dim", 0)) != dim:         # ③
            raise ValueError(f"cache_dim 不符：manifest={m.get('cache_dim')} cache={dim}")
        entries = list(cj.get("entries") or [])
        if len(raw) != len(entries) * dim * 4:        # 截断/半成品必须被拒
            raise ValueError(f"cache.f32 长度 {len(raw)} != {len(entries)}×{dim}×4")
        row_of = {str(k): i for i, (k, _g) in enumerate(entries)}
        # Materialize active rows in manifest order.
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
        """Return a view only when both namespace and ordered corpus fingerprint match."""
        with self._lock:
            if self._view is None or self._view_fp != fingerprint:
                return None
            if not self._space_matches():
                return None
            return self._view

    def view(self) -> VectorView | None:
        """Return the current namespace-matching view for health and diagnostics."""
        with self._lock:
            if self._view is None or not self._space_matches():
                return None
            return self._view

    def _space_matches(self) -> bool:
        try:
            return self._view_space == space_from_settings()
        except InputProfileError:
            return False

    def missing(self) -> list[str]:
        with self._lock:
            return list(self._missing)

    def last_error(self) -> str | None:
        with self._lock:
            return self._last_error

    # ── 写 ────────────────────────────────────────────────────────────

    def update(self, chunks: list[dict], space: Space) -> dict:
        """Reuse unchanged inputs and publish an atomic generation; skip a busy writer."""
        self.root.mkdir(parents=True, exist_ok=True)
        profile = active_profile()
        texts = [document_text(c.get("title", ""), c.get("text", ""), profile) for c in chunks]
        keys = [chunk_key(space, t) for t in texts]
        fp = corpus_fingerprint(space, chunks)

        with _Flock(self.root / LOCK) as got:
            if not got:
                logger.debug("向量更新：另一个 worker 正在更新，本轮跳过")
                return {"skipped": "locked", "fingerprint": fp,
                        "embedded": 0, "reused": 0, "api_calls": 0}
            self.load()          # Observe the current committed generation.
            m = _read_json(self.manifest_path) or {}
            if (m.get("fingerprint") == fp and not m.get("missing")
                    and int(m.get("count", -1)) == len(keys)
                    and self.view_for(fp) is not None):
                # Compare physical row counts against chunk slots, including empty chunks.
                # A dictionary of deduplicated keys can hide a vector/key alignment mismatch.
                return {"skipped": "aligned", "fingerprint": fp, "embedded": 0,
                        "reused": len(set(keys)), "api_calls": 0}
            return self._update_locked(texts, keys, space, fp)

    def _cache_for_space(self, space, manifest, reset):
        if reset or Space.from_dict(manifest.get("space") or {}) != space:
            return {}, 0, {}
        return self._read_cache()

    def _update_locked(self, texts, keys, space, fp, reset=False) -> dict:
        """Embed missing inputs under flock and advance the monotonic generation counter."""
        m0 = _read_json(self.manifest_path) or {}
        old_keys, old_dim, old_rows = self._cache_for_space(space, m0, reset)
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
                # Empty inputs retain a zero row so chunk indices remain aligned.
                empty_keys.add(key)
                continue
            pending.append((key, text))

        embedded: list[tuple[str, array]] = []
        failed: list[str] = []
        if pending:
            vecs = _embed_texts([t for _, t in pending], space)
            try:
                embedded, failed = _materialize(pending, vecs, old_dim, space.dim)
            except DimensionChanged:
                return self._update_locked(texts, keys, space, fp, reset=True)

        dim = old_dim or (len(embedded[0][1]) if embedded else 0)
        if dim <= 0:
            # 一条都没嵌成、也没有旧库 ⇒ 不写半份产物，诚实报错
            self._last_error = "api_error" if pending else "no_vectors"
            return {"fingerprint": fp, "embedded": 0, "reused": reused,
                    "api_calls": _batches(len(pending)), "missing": len(failed), "ok": False}

        # Keep one row per chunk; failed zero rows are retried on the next update.
        keep: dict[str, tuple[array, int]] = {}
        for key in seen:
            row = old_rows.get(key)
            if row is not None:
                keep[key] = (row, gen)
        for key, row in embedded:
            keep[key] = (row, gen)
        zeros: array | None = None
        for key in empty_keys | set(failed):
            if key not in keep:
                zeros = zeros if zeros is not None else array("f", [0.0] * dim)
                keep[key] = (zeros, gen)
        # Retain unreferenced keys only within the configured generation window.
        for key, g in old_keys.items():
            if key not in keep and int(g) >= gen - PRUNE_KEEP_GENS:
                keep[key] = (old_rows[key], int(g))

        order = [(k, g) for k, (_r, g) in keep.items()]
        cache_files = write_generation(self.root,
            b"".join(r.tobytes() for _k, (r, _g) in keep.items()),
            {"schema": SCHEMA, "dim": dim, "entries": order}, _write_atomic)
        active_keys = [k for k in keys if k in keep]
        manifest = {
            "schema": SCHEMA, "gen": gen, "space": space.as_dict(),
            "cache_files": cache_files,
            "previous_caches": generation_history(m0),
            "fingerprint": fp, "count": len(active_keys), "keys": active_keys,
            "cache_dim": dim, "missing": failed,
            "built_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "stats": {"embedded": len(embedded), "reused": reused,
                      "api_calls": _batches(len(pending)),
                      "pruned": len(old_keys) - sum(1 for k in keep if k in old_keys)},
        }
        _write_atomic(self.manifest_path,
                      json.dumps(manifest, ensure_ascii=False).encode("utf-8"))
        prune_generations(self.root, manifest)
        self._last_error = None
        logger.info("向量索引已更新：新嵌 %d / 复用 %d / API %d 批 / 维度 %d / 缺 %d",
                    len(embedded), reused, _batches(len(pending)), dim, len(failed))
        self.load()      # Install the committed generation.
        return {"fingerprint": fp, "embedded": len(embedded), "reused": reused,
                "api_calls": _batches(len(pending)), "missing": len(failed),
                "dim": dim, "ok": True}

    # ── 缓存读写 ──────────────────────────────────────────────────────

    def _read_cache(self) -> tuple[dict[str, int], int, dict[str, array]]:
        """Load cached keys, generations and vectors; treat corrupt generations as empty."""
        m = _read_json(self.manifest_path)
        if not m or m.get("schema") != SCHEMA:
            return {}, 0, {}
        try:
            cj, raw = read_generation(self.root, m)
            dim = int(cj["dim"])
            entries = cj["entries"]
            if dim <= 0 or len(raw) != len(entries) * dim * 4:
                raise ValueError("invalid vector cache size")
        except (OSError, ValueError, KeyError, TypeError):
            logger.warning("Invalid vector generation; rebuilding from source")
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


# ══════════════════════════════════════════════════════════════════════════
#  进程级单例 + 查询侧
# ══════════════════════════════════════════════════════════════════════════

_store: VectorStore | None = None
_store_lock = threading.Lock()
_warm_lock = threading.Lock()
_query_cache: dict[tuple[Space, str], array] = {}
_query_order: list[tuple[Space, str]] = []
_query_lock = threading.Lock()


def get_store() -> VectorStore:
    global _store
    with _store_lock:
        if _store is None:
            _store = VectorStore(_settings.rag_vector_dir)
        return _store


def is_enabled() -> bool:
    """Require both the hybrid switch and configured embedding credentials."""
    return bool(_settings.rag_hybrid_enabled) and bool(_settings.embedding_configured)


def degraded_reason() -> str | None:
    """Expose vector-route health separately from the shared candidate format.

    None means the configured route is healthy or explicitly disabled."""
    if not _settings.rag_hybrid_enabled:
        return None                     # 关着是设定，不是降级
    if not _settings.embedding_configured:
        return "missing_credentials"
    try:
        active_profile()
    except InputProfileError:
        return "embedding_profile_error"
    st = get_store()
    st.load()       # Observe generations published by other workers.
    if st.last_error():
        return st.last_error()
    if st.view() is None:
        return "warming"
    if st.missing():
        return "vector_missing"
    return None


def route_status() -> dict:
    """Return non-secret route facts for tool metadata and health checks.

    state distinguishes off, active and degradation reasons. vector_missing may
    coexist with active=True when a partial index is still queryable."""
    st = get_store()
    st.load()
    view = st.view()
    reason = degraded_reason()
    if not _settings.rag_hybrid_enabled:
        state = "off"
    else:
        state = reason or "active"
    return {"enabled": bool(_settings.rag_hybrid_enabled),
            "active": bool(is_enabled() and view is not None),
            "state": state,
            "reason": reason,
            "vectors": 0 if view is None else len(view),
            "missing": len(st.missing())}


def embed_query(query: str) -> array | None:
    """Bounded LRU, keyed by both embedding space and normalized query."""
    q = (query or "").strip()
    if not q or len(q) > 32768:
        return None
    try:
        space = space_from_settings()
        q = query_text(q, active_profile())
    except InputProfileError:
        return None
    st = get_store()
    view = st.view()
    dim = view.dim if view is not None else 0
    key = (space, q)
    with _query_lock:
        hit = _query_cache.get(key)
        if (hit is not None and (not dim or len(hit) == dim)
                and (not space.dim or len(hit) == space.dim)):
            _query_order.remove(key)
            _query_order.append(key)
            return hit
    try:
        got = _embed_texts([q], space)[0]
    except (ValueError, TypeError, IndexError, OverflowError):
        logger.warning("Query embedding failed", exc_info=True)
        return None
    if (not got or not any(got) or not all(math.isfinite(x) for x in got)
            or (dim and len(got) != dim) or (space.dim and len(got) != space.dim)):
        return None
    vec = array("f", got)
    if not any(vec) or not all(math.isfinite(x) for x in vec):
        return None
    cap = max(1, int(_settings.embedding_query_cache or 256))
    with _query_lock:
        if key in _query_order:
            _query_order.remove(key)
        _query_cache[key] = vec
        _query_order.append(key)
        while len(_query_order) > cap:
            _query_cache.pop(_query_order.pop(0), None)
    return vec


def warm_async(chunks: list[dict]) -> None:
    """Run at most one background vector update without blocking corpus publication."""
    if not is_enabled():
        return
    if not _warm_lock.acquire(blocking=False):
        return

    def _run() -> None:
        try:
            get_store().update(vector_chunks(chunks), space_from_settings())
        except Exception:                             # noqa: BLE001
            logger.warning("向量索引后台更新失败（下一轮刷新重试）", exc_info=True)
        finally:
            _warm_lock.release()

    threading.Thread(target=_run, name="rag-vector-warm", daemon=True).start()
