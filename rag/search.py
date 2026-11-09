"""Article retrieval over complete, versioned public corpus snapshots.

Index text preserves heading paths, chunks have bounded source ranges, and BM25
uses total token counts consistently. Optional vector retrieval uses the same text.
Failures retain the last complete snapshot for a bounded grace period and expose
staleness; a failed page is never interpreted as an empty or complete corpus.
"""
from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable

from config.settings import settings
from rag import DEFAULT_TOP_K, MAX_TOP_K
from rag.chunks import split as split_chunks
from rag.corpus import corpus_revision
from rag.corpus import fetch as fetch_corpus
from rag.embedding_inputs import InputProfileError, vector_chunks
from rag.lexical import LexicalIndex, backend_info
from rag.ranking import documents as rank_documents
from rag.sections import reading_revision
from rag.tokenization import tokenize
from rag.vector_index import (
    chunk_text,
    corpus_fingerprint,
    degraded_reason,
    embed_query,
    get_store,
    is_enabled,
    rrf_fuse,
    space_from_settings,
)

# Vector warming runs independently of corpus and lexical index refresh.
from rag.vector_index import warm_async as warm_vectors

logger = logging.getLogger(__name__)

BM25_K1 = 1.2
BM25_B = 0.75

REFRESH_TTL = 600.0


def candidate_limit(top_k: int) -> int:
    """Keep production and evaluation per-retriever candidate windows aligned."""
    return min(100, max(20, top_k * 3))

# Reviewed query variants contribute only tokens present in the current vocabulary.
_QUERY_SYNONYMS: tuple[dict, ...] = (
    {"key": "测评", "val": "评测", "why": "将访客措辞映射为站内评测术语。"},
    {"key": "令牌", "val": "JWT", "why": "将中文鉴权用语映射为文档中的协议名。"},
    {"key": "断线", "val": "断连", "why": "统一连接中断的领域别名。"},
    {"key": "空中升级", "val": "OTA", "why": "将固件升级的中文别名映射为文档缩写。"},
)


def synonyms() -> tuple[dict, ...]:
    """Return the configured domain aliases and their rationale."""
    return _QUERY_SYNONYMS


# Evaluation may compare expansion on/off; production uses the enabled setting.
_EXPANSION_ENABLED = True


def set_expansion(on: bool) -> None:
    """Enable domain aliases for retrieval evaluation."""
    global _EXPANSION_ENABLED
    _EXPANSION_ENABLED = bool(on)


def expansion_enabled() -> bool:
    return _EXPANSION_ENABLED




def chunk_note(title: str, content: str) -> list[dict]:
    """Bounded, heading-aware chunks; source ranges support selective reads."""
    return split_chunks(title, content)


# Record the actual retrieval route per worker thread, including degraded vector reads.
_route = threading.local()


def _record(mode: str, reason: str | None, vectors: int = 0, missing: int = 0) -> None:
    from utils.trace import record

    _route.last = {"mode": mode, "reason": reason,
                   "vectors": vectors, "missing": missing}
    record("rag", "retrieval", **_route.last)


def last_route() -> dict:
    """Return the retrieval mode and degradation reason for this thread."""
    return dict(getattr(_route, "last",
                        {"mode": "lexical", "reason": None, "vectors": 0, "missing": 0}))


def _mode_for(reason: str | None) -> str:
    """Distinguish a disabled/empty vector route from an unavailable route."""
    return "lexical" if reason in (None, "no_vector_hits") else "degraded"


def _rrf_k() -> int:
    """Read the configured RRF constant, defaulting to 60."""
    return int(getattr(settings, "rrf_k", 60) or 60)


class RagIndex:
    """刷新公开语料与 BM25S 快照；完整构建后原子替换，向量在后台预热。"""

    def __init__(
        self,
        *,
        corpus_loader: Callable[[], list[dict]] | None = None,
        vector_warmer: Callable[[list[dict]], None] | None = None,
    ) -> None:
        self._corpus_loader = corpus_loader
        self._vector_warmer = vector_warmer
        self._lock = threading.Lock()
        self._build_lock = threading.Lock()
        self._refresh_lock = threading.Lock()
        self._last_attempt = 0.0
        self._last_error: str | None = None
        self._revision = ""
        self._dirty = False
        self._invalidations = 0
        self._docs: list[dict] = []
        self._chunks: list[dict] = []
        self._vector_chunk_cache: tuple | None = None
        self._lexical: LexicalIndex | None = None
        self._last_build = 0.0

    def build(self) -> bool:
        if not self._build_lock.acquire(blocking=False):
            return False
        self._last_attempt = time.time()
        try:
            self._build()
            return True
        except Exception as exc:
            self._last_error = type(exc).__name__
            raise
        finally:
            self._build_lock.release()

    def warm(self) -> bool:
        """Publish a complete snapshot or retain the previous one with failure telemetry."""
        try:
            return self.build()
        except Exception:
            logger.warning("Corpus refresh failed; retaining the last complete snapshot",
                           exc_info=True)
            return False

    def refresh_async(self) -> None:
        """Serve the published snapshot while at most one refresh is scheduled."""
        if not self._refresh_lock.acquire(blocking=False):
            return

        def refresh() -> None:
            try:
                self.warm()
            finally:
                self._refresh_lock.release()

        try:
            threading.Thread(target=refresh, name="rag-warm", daemon=True).start()
        except BaseException:
            self._refresh_lock.release()
            raise

    def _build(self) -> None:
        invalidations = self._invalidations
        docs = self._corpus_loader() if self._corpus_loader else self._fetch_corpus()
        chunks: list[dict] = []
        for d in docs:
            read_revision = reading_revision(d["title"], d["content"])
            for c in chunk_note(d["title"], d["content"]):
                chunks.append({**d, **c, "reading_revision": read_revision})

        lexical = LexicalIndex([tokenize(chunk_text(c["title"], c["text"])) for c in chunks],
                               k1=BM25_K1, b=BM25_B)
        revision = corpus_revision(docs)

        with self._lock:
            self._docs, self._chunks = docs, chunks
            self._vector_chunk_cache = None
            self._lexical, self._revision = lexical, revision
            self._last_error = None
            self._dirty = self._invalidations != invalidations
            if self._dirty:
                self._last_attempt = 0.0
            self._last_build = time.time()
        # Embedding warmup runs separately after publication of the complete lexical snapshot.
        self._warm_vectors(chunks)

    def _warm_vectors(self, chunks: list[dict]) -> None:
        if self._vector_warmer is None:
            warm_vectors(chunks)
        else:
            self._vector_warmer(chunks)

    def _fetch_corpus(self) -> list[dict]:
        from functools import partial

        from tools.base import _get
        from tools.http_channel import CORPUS_RESPONSE_BYTES

        return fetch_corpus(partial(_get, max_response_bytes=CORPUS_RESPONSE_BYTES))

    def status(self) -> dict:
        with self._lock:
            return {"revision": self._revision, "documents": len(self._docs),
                    "stale": bool(self._last_error) or self.is_stale(),
                    "error": self._last_error, "last_success": self._last_build,
                    "lexical_backend": backend_info()}

    def docs_snapshot(self) -> list[dict]:
        """Return the currently published public document snapshot."""
        with self._lock:
            return list(self._docs)

    def vocabulary(self) -> frozenset[str]:
        """Read the vocabulary of the currently published lexical snapshot."""
        with self._lock:
            return self._lexical.vocabulary if self._lexical is not None else frozenset()

    def is_stale(self) -> bool:
        """Report whether the snapshot is dirty, missing or past its refresh TTL."""
        return self._dirty or not self._last_build or time.time() - self._last_build > REFRESH_TTL

    def _lexical_ranked(self, query: str, top_k: int, *,
                        article_id: int | None = None) -> list[dict] | None:
        """Return raw-query BM25 candidates bounded by top_k; None means unavailable."""
        if self.is_stale() and time.time() - self._last_attempt > 30:
            # Once the grace window is exhausted, refresh like a cold start:
            # an async refresh would reject this query before its healthy result arrives.
            if self._last_build and time.time() - self._last_build <= REFRESH_TTL * 2:
                self.refresh_async()
            else:
                self.warm()
        with self._lock:
            if not self._last_build or time.time() - self._last_build > REFRESH_TTL * 2:
                return None
            if not self._chunks:
                return []
            chunks, lexical, revision = self._chunks, self._lexical, self._revision
        assert lexical is not None
        vocabulary = lexical.vocabulary
        # Deduplicate query terms without changing corpus frequencies.
        q_toks = list(dict.fromkeys(t for t in tokenize(query) if t in vocabulary))
        if _EXPANSION_ENABLED:
            for _pair in _QUERY_SYNONYMS:
                if _pair["key"] in query:
                    q_toks += [
                        t for t in tokenize(query.replace(_pair["key"], _pair["val"]))
                        if t not in q_toks and t in vocabulary]
        q_toks = list(dict.fromkeys(q_toks))
        if not q_toks:
            return []
        allowed = None if article_id is None else [
            i for i, chunk in enumerate(chunks)
            if chunk["type"] == "note" and chunk["id"] == article_id]
        scores = lexical.scores(q_toks, allowed_indices=allowed)
        # Document score is its strongest matching chunk; retain section locations.
        ranked = rank_documents(chunks, scores, revision, query_terms=q_toks, limit=top_k)
        for r in ranked:
            r["score"] = round(r["score"], 4)
        return ranked

    def search(self, query: str, top_k: int = DEFAULT_TOP_K, *,
               article_id: int | None = None) -> list[dict] | None:
        """Rank lexical candidates and fuse only corpus-aligned vector candidates."""
        if type(top_k) is not int or not 1 <= top_k <= MAX_TOP_K:
            raise ValueError(f"top_k must be an integer between 1 and {MAX_TOP_K}")
        candidates = candidate_limit(top_k)
        lex = self._lexical_ranked(query, candidates, article_id=article_id)
        if lex is None:
            unavailable_reason = self._last_error or ("refresh_in_progress"
                if self._build_lock.locked() else "snapshot_expired")
            _record("unavailable", unavailable_reason)
            return None
        vec, reason, miss = self._vector_ranked(query, candidates, article_id=article_id)
        if not vec:
            # Disabled, empty and failed vector routes have distinct telemetry.
            _record(_mode_for(reason), reason, missing=miss)
            return lex[:top_k]
        if lex and lex[0].get("corpus_revision") != vec[0].get("corpus_revision"):
            _record("degraded", "corpus_changed", missing=miss)
            return lex[:top_k]
        try:
            fused = rrf_fuse(lex, vec, _rrf_k(), top_k)
        except ValueError:
            _record("degraded", "corpus_changed", missing=miss)
            return lex[:top_k]
        _record("hybrid", None, len(vec), miss)
        return fused or lex[:top_k]

    def _vector_chunks_for(self, chunks: list[dict], space) -> list[dict]:
        with self._lock:
            cached = self._vector_chunk_cache
        if cached is not None and cached[0] is chunks and cached[1] == space:
            return cached[2]
        refined = vector_chunks(chunks)
        with self._lock:
            if self._chunks is chunks:
                self._vector_chunk_cache = (chunks, space, refined)
        return refined

    def _vector_ranked(self, query: str, top_k: int, *,
                       article_id: int | None = None) -> tuple[list[dict], str | None, int]:
        """Return document candidates, degradation reason and missing-vector count."""
        if not is_enabled():
            return [], (degraded_reason() if settings.rag_hybrid_enabled else None), 0
        with self._lock:
            chunks = self._chunks
            revision = self._revision
        try:
            space = space_from_settings()
            chunks = self._vector_chunks_for(chunks, space)
        except InputProfileError:
            return [], "embedding_profile_error", 0
        store = get_store()
        store.load()
        view = store.view_for(corpus_fingerprint(space, chunks))
        if view is None:
            self._warm_vectors(chunks)
            return [], degraded_reason() or "no_vectors", len(store.missing())
        if len(view) != len(chunks):
            logger.warning("向量视图 %d 行 != 语料 %d chunk——本轮退回纯词法",
                           len(view), len(chunks))
            return [], "stale_view", len(store.missing())
        if not chunks or article_id is not None and not any(
            chunk["type"] == "note" and chunk["id"] == article_id for chunk in chunks
        ):
            return [], "no_vector_hits", 0
        qv = embed_query(query)
        if qv is None:
            return [], "query_embed_failed", len(store.missing())
        from sqlalchemy.exc import SQLAlchemyError

        from rag.postgres import StaleSnapshot

        try:
            scores = view.scores(qv, max(1, top_k), article_id=article_id)
        except StaleSnapshot:
            return [], "corpus_changed", len(store.missing())
        except (SQLAlchemyError, ValueError):
            logger.warning("PostgreSQL vector query is unavailable")
            store.record_failure()
            return [], "vector_database_unavailable", len(store.missing())
        ranked = rank_documents(chunks, scores, revision, retriever="vector",
                                query_terms=tokenize(query), limit=top_k)
        for r in ranked:
            r["score"] = round(r["score"], 4)
        return ranked, (None if ranked else "no_vector_hits"), len(store.missing())


_index: RagIndex | None = None
_index_lock = threading.Lock()


def get_index() -> RagIndex:
    global _index
    with _index_lock:
        if _index is None:
            _index = RagIndex()
        return _index


def invalidate_document(article_id: int, revision: str) -> None:
    """An authoritative revision mismatch invalidates only a matching cached article."""
    idx = get_index()
    with idx._lock:
        if not any(d["id"] == article_id and reading_revision(d["title"], d["content"]) == revision
                   for d in idx._docs):
            return
        idx._dirty = True
        idx._invalidations += 1
        idx._last_attempt = 0.0
    warm_async()


def search(query: str, top_k: int = DEFAULT_TOP_K, *,
           article_id: int | None = None) -> list[dict] | None:
    """Return ranked article candidates with readable source locators, without full text."""
    return get_index().search(query, top_k=top_k, article_id=article_id)


def warm() -> bool:
    """Build the corpus snapshot synchronously; return False on failure."""
    return get_index().warm()



def warm_async() -> None:
    """Schedule the singleton index through the same bounded refresh path as queries."""
    get_index().refresh_async()



if __name__ == "__main__":
    import sys
    idx = get_index()
    idx.build()
    q = sys.argv[1] if len(sys.argv) > 1 else "ESP32-S3 OTA 更新需要哪些分区？"
    for h in idx.search(q) or []:
        print(f"  {h['type']}:{h['id']:<4} [{h['sections']}] {h['title'][:24]}")
