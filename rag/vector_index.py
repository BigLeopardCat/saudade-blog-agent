"""Incremental embeddings and PostgreSQL-backed public-corpus retrieval."""

from __future__ import annotations

import hashlib
import json
import logging
import math
import threading
import time
from array import array
from contextvars import ContextVar
from dataclasses import dataclass, field

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
from rag.postgres import PostgresRepository, Snapshot

logger = logging.getLogger(__name__)

SEP = "\x1f"


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


def _merge_locations(target, row):
    """Keep each section label and its source ranges attached to the section identity."""
    for section, ident in zip(row.get("sections") or [], row.get("section_ids") or []):
        location = target.setdefault(ident, {"section": section, "section_id": ident,
                                            "matches": []})
        for match in row.get("matches") or []:
            if match.get("section_id") == ident and match not in location["matches"]:
                location["matches"].append(dict(match))


def rrf_fuse(lex: list[dict], vec: list[dict], k: int, top_k: int) -> list[dict]:
    """Use official LangChain RRF ordering and preserve source-locator relationships."""
    from langchain_classic.retrievers.ensemble import EnsembleRetriever
    from langchain_core.documents import Document
    from langchain_core.runnables import RunnableLambda

    info: dict[tuple, dict] = {}
    ranked_documents = []
    for ranked in (lex, vec):
        documents = []
        for item in ranked:
            key = (item["type"], item["id"])
            documents.append(Document(page_content="", metadata={"identity": key}))
            if key not in info:
                info[key] = {**item, "_locations": {}}
                info[key].pop("score", None)
            _merge_locations(info[key]["_locations"], item)
        ranked_documents.append(documents)

    def retriever_for(rows: list[Document]) -> RunnableLambda:
        return RunnableLambda(lambda _: rows)

    ensemble = EnsembleRetriever(
        retrievers=[retriever_for(rows) for rows in ranked_documents],
        weights=[0.5, 0.5], c=k, id_key="identity",
    )
    out = []
    for rank, document in enumerate(ensemble.invoke("")[:max(1, top_k)], 1):
        row = dict(info[document.metadata["identity"]], rank=rank)
        locations = list(row.pop("_locations").values())[:3]
        row["locators"] = locations
        row["sections"] = [location["section"] for location in locations[:2]]
        row["section_ids"] = [location["section_id"] for location in locations[:2]]
        row["matches"] = [match for location in locations for match in location["matches"]][:3]
        out.append(row)
    return out


# ── embedding 客户端（OpenAI 兼容；供应商不进代码）──────────────────────

_client = None
_client_sig: tuple | None = None
_client_lock = threading.Lock()
_http_requests: ContextVar[int | None] = ContextVar("embedding_http_requests", default=None)


def _observe_request(request):
    """Count HTTP attempts at the transport boundary without retaining request content."""
    count = _http_requests.get()
    if count is not None:
        _http_requests.set(count + 1)
    from utils.trace import record

    record("rag", "embedding_http_request")


def _get_client():
    """Reuse a single-attempt SDK client; failed retrieval falls back to lexical search."""
    global _client, _client_sig
    sig = (_settings.embedding_api_key, _settings.embedding_base_url,
           float(_settings.embedding_timeout))
    with _client_lock:
        if _client is None or _client_sig != sig:
            from openai import DefaultHttpxClient, OpenAI
            kwargs: dict = {"api_key": sig[0], "timeout": sig[2], "max_retries": 0,
                            "http_client": DefaultHttpxClient(
                                timeout=sig[2], event_hooks={"request": [_observe_request]})}
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
    from agent.runtime.control import model_options

    resp = client.embeddings.create(**kwargs, **model_options(float(_settings.embedding_timeout)))
    ordered = sorted(resp.data, key=lambda v: v.index)
    if [v.index for v in ordered] != list(range(len(texts))):
        raise ValueError("Invalid embedding response indexes")
    got = [v.embedding for v in ordered]
    if len(got) != len(texts):
        # Partial responses cannot be aligned with the input batch.
        raise ValueError(f"期望 {len(texts)} 条，API 返回 {len(got)} 条")
    return got


def _batches(n: int) -> int:
    """Count logical input batches, independently of actual HTTP attempts."""
    size = max(1, int(_settings.embedding_batch_size or 10))
    return (n + size - 1) // size if n > 0 else 0


def _endpoint_failed(error):
    """Stop a run after shared transport, authentication, capacity or service failures."""
    from openai import APIConnectionError, APIStatusError

    return (isinstance(error, APIConnectionError)
            or isinstance(error, APIStatusError) and (
                error.status_code in {401, 403, 404, 408, 409, 429} or error.status_code >= 500))


def _embed_texts(texts: list[str], space: Space) -> list[list[float] | None]:
    """Use one request per batch; split only a multi-input HTTP 413 rejection."""
    from openai import APIStatusError

    from agent.runtime.budget import BudgetExceeded
    from agent.runtime.control import AgentCancelled, check_active

    validate_inputs(texts, space.profile)
    out: list[list[float] | None] = []
    size = max(1, int(_settings.embedding_batch_size or 10))
    for i in range(0, len(texts), size):
        check_active()
        batch = texts[i:i + size]
        try:
            got: list[list[float] | None] = [*_embed_raw(batch, space)]
        except (AgentCancelled, BudgetExceeded, TimeoutError):
            raise
        except Exception as error:  # noqa: BLE001
            # Retrying an outage per input amplifies failures. A 413 alone may be
            # resolved by reducing the batch; single inputs are never resent.
            if _endpoint_failed(error):
                logger.warning("Embedding endpoint failed (%s)", type(error).__name__)
                return out + [None] * (len(texts) - len(out))
            got = [None] * len(batch)
            if len(batch) > 1 and isinstance(error, APIStatusError) and error.status_code == 413:
                got = []
                for text in batch:
                    check_active()
                    try:
                        got.append(_embed_raw([text], space)[0])
                    except (AgentCancelled, BudgetExceeded, TimeoutError):
                        raise
                    except Exception as single:  # noqa: BLE001
                        logger.warning("Embedding input failed (%s)", type(single).__name__)
                        if _endpoint_failed(single):
                            return out + got + [None] * (len(texts) - len(out) - len(got))
                        got.append(None)
            else:
                logger.warning("Embedding batch failed (%s)", type(error).__name__)
        out.extend(got)
    return out


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


class VectorView:
    """A fingerprint-bound view; all distance and document ranking execute in PostgreSQL."""

    def __init__(self, repository, snapshot: Snapshot):
        self.repository, self.snapshot = repository, snapshot
        self.dim = snapshot.dimension

    def __len__(self):
        return self.snapshot.count

    def scores(self, query, top_k):
        return self.repository.scores(self.snapshot, list(query), top_k)


class VectorStore:
    """Reuse content keys; publish complete public membership through a database transaction."""

    def __init__(self, repository):
        self.repository = repository
        self._lock = threading.Lock()
        self._snapshot: Snapshot | None = None
        self._missing: list[str] = []
        self._last_error: str | None = None
        self._reset_dimensions = False

    def close(self):
        self.repository.close()

    def load(self) -> bool:
        from sqlalchemy.exc import SQLAlchemyError

        try:
            snapshot = self.repository.snapshot()
        except (SQLAlchemyError, ValueError):
            logger.warning("PostgreSQL vector index is unavailable")
            with self._lock:
                self._snapshot = None
                self._last_error = "vector_database_unavailable"
            return False
        with self._lock:
            if snapshot is not None and snapshot != self._snapshot:
                self._last_error = None
                self._missing = []
            self._snapshot = snapshot
            return self.view() is not None

    def view_for(self, fingerprint: str) -> VectorView | None:
        view = self.view()
        return view if view is not None and view.snapshot.fingerprint == fingerprint else None

    def view(self) -> VectorView | None:
        snapshot = self._snapshot
        try:
            if snapshot is None or snapshot.space != space_from_settings().as_dict():
                return None
        except InputProfileError:
            return None
        return VectorView(self.repository, snapshot)

    def missing(self) -> list[str]:
        return list(self._missing)

    def last_error(self) -> str | None:
        return self._last_error

    def invalidate_dimension(self):
        self._reset_dimensions = True
        self._last_error = "vector_dimension_changed"

    def available(self, space: Space) -> dict[str, int]:
        key = hashlib.sha256(json.dumps(space.as_dict(), sort_keys=True).encode()).hexdigest()
        snapshot = self._snapshot
        dimension = (snapshot.dimension if snapshot is not None and
                     snapshot.space == space.as_dict() else space.dim)
        return self.repository.available(key, dimension)

    def update(self, chunks: list[dict], space: Space) -> dict:
        """Do model I/O outside transactions and never publish an incomplete snapshot."""
        profile = active_profile()
        texts = [document_text(c.get("title", ""), c.get("text", ""), profile) for c in chunks]
        keys = [chunk_key(space, value) for value in texts]
        fingerprint = corpus_fingerprint(space, chunks)
        with self.repository.writer() as acquired:
            if not acquired:
                return {"skipped": "locked", "fingerprint": fingerprint,
                        "embedded": 0, "reused": 0, "api_calls": 0, "logical_batches": 0}
            self.load()
            if self.view_for(fingerprint) is not None and not self._reset_dimensions:
                return {"skipped": "aligned", "fingerprint": fingerprint,
                        "embedded": 0, "reused": len(set(keys)), "api_calls": 0,
                        "logical_batches": 0}
            token = _http_requests.set(0)
            try:
                return self._update(chunks, texts, keys, space, fingerprint)
            finally:
                _http_requests.reset(token)

    def _update(self, chunks, texts, keys, space, fingerprint, logical_batches=0):
        cached = {} if self._reset_dimensions else self.available(space)
        current = self._snapshot
        dimension = (current.dimension if current is not None and
                     current.space == space.as_dict() and not self._reset_dimensions else space.dim)
        if not dimension and cached:
            dimensions = set(cached.values())
            dimension = dimensions.pop() if len(dimensions) == 1 else 0
        cached = {key: value for key, value in cached.items() if value == dimension}
        unique = dict(zip(keys, texts, strict=True))
        pending = [(key, value) for key, value in unique.items() if key not in cached]
        logical_batches += _batches(len(pending))
        values = _embed_texts([value for _, value in pending], space) if pending else []
        try:
            embedded, failed = _materialize(pending, values, dimension, space.dim)
        except DimensionChanged:
            self._reset_dimensions = True
            return self._update(chunks, texts, keys, space, fingerprint, logical_batches)
        dimension = dimension or (len(embedded[0][1]) if embedded else 0)
        reused = len(set(keys) & cached.keys())
        complete = not failed and dimension > 0
        stats = {"embedded": len(embedded), "reused": reused,
                 "api_calls": _http_requests.get(), "logical_batches": logical_batches}
        space_key = hashlib.sha256(json.dumps(space.as_dict(), sort_keys=True).encode()).hexdigest()
        from agent.runtime.control import check_active

        check_active()
        self.repository.commit(
            rows=[{"key": key, "dimension": len(value), "embedding": list(value)}
                  for key, value in embedded], chunks=chunks, keys=keys, space=space.as_dict(),
            space_key=space_key, fingerprint=fingerprint, dimension=dimension, stats=stats,
            complete=complete,
        )
        self._missing = failed
        self._last_error = None if complete else "vector_missing"
        if complete:
            self._reset_dimensions = False
            self.load()
        return {**stats, "fingerprint": fingerprint, "missing": len(failed),
                "dim": dimension, "ok": complete}


# ── 进程级单例 + 查询侧 ───────────────────────────────────────────────

_store: VectorStore | None = None
_store_lock = threading.Lock()
_warm_lock = threading.Lock()
_query_cache: dict[tuple[Space, str], array] = {}
_query_order: list[tuple[Space, str]] = []
_query_lock = threading.Lock()
_submit = None
_warm_stop = threading.Event()


def get_store() -> VectorStore:
    global _store
    with _store_lock:
        if _store is None:
            _store = VectorStore(PostgresRepository(
                _settings.rag_vector_dsn, _settings.rag_vector_collection))
        return _store


def is_enabled() -> bool:
    """Require the switch, embedding model and explicit PostgreSQL configuration."""
    return (bool(_settings.rag_hybrid_enabled) and bool(_settings.embedding_configured)
            and bool(_settings.rag_vector_dsn.strip()))


def degraded_reason() -> str | None:
    """Expose vector-route health separately from the shared candidate format.

    None means the configured route is healthy or explicitly disabled."""
    if not _settings.rag_hybrid_enabled:
        return None                     # 关着是设定，不是降级
    if not _settings.embedding_configured:
        return "missing_credentials"
    if not _settings.rag_vector_dsn.strip():
        return "missing_vector_database"
    try:
        active_profile()
    except InputProfileError:
        return "embedding_profile_error"
    st = get_store()
    st.load()
    if st.last_error():
        return st.last_error()
    if st.view() is None:
        return "warming"
    if st.missing():
        return "vector_missing"
    return None


def route_status() -> dict:
    """Return non-secret route facts for tool metadata and health checks.

    A failed new build can coexist with an older complete active snapshot."""
    if not is_enabled():
        return {"enabled": bool(_settings.rag_hybrid_enabled), "active": False,
                "state": degraded_reason() or "off", "reason": degraded_reason(),
                "vectors": 0, "missing": 0}
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
    if got and dim and len(got) != dim:
        st.invalidate_dimension()
        return None
    if (not got or not any(got) or not all(math.isfinite(x) for x in got)
            or (space.dim and len(got) != space.dim)):
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


def start_background(submit):
    """Use the application's existing bounded executor for optional vector builds."""
    global _submit, _warm_stop
    _warm_stop = threading.Event()
    _submit = submit


def stop_background():
    global _submit
    _submit = None
    _warm_stop.set()


def close():
    """Call after application workers have drained, before process resources disappear."""
    global _store, _client, _client_sig
    stop_background()
    if _store is not None:
        _store.close()
        _store = None
    if _client is not None:
        _client.close()
        _client, _client_sig = None, None


def warm_async(chunks: list[dict]) -> None:
    """Submit at most one build to the application-owned worker pool."""
    submit = _submit
    if not is_enabled() or submit is None or not _warm_lock.acquire(blocking=False):
        return
    stop = _warm_stop

    def _run() -> None:
        from agent.runtime.control import AgentCancelled, RequestControl, request_scope

        try:
            control = RequestControl(stop, time.monotonic() + _settings.agent_task_seconds)
            with request_scope(control):
                get_store().update(vector_chunks(chunks), space_from_settings())
        except AgentCancelled:
            pass
        except Exception:  # noqa: BLE001
            logger.warning("Vector background update failed", exc_info=True)
        finally:
            _warm_lock.release()

    try:
        submit(_run)
    except RuntimeError:
        _warm_lock.release()
