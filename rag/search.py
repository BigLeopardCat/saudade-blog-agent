"""Article retrieval over complete, versioned public corpus snapshots.

Index text preserves heading paths, chunks have bounded source ranges, and BM25
uses total token counts consistently. Optional vector retrieval uses the same text.
Failures retain the last complete snapshot for a bounded grace period and expose
staleness; a failed page is never interpreted as an empty or complete corpus.
"""
from __future__ import annotations

import logging
import re
import threading
import time
from collections.abc import Callable

from config.settings import settings
from rag.chunks import split as split_chunks
from rag.corpus import corpus_revision
from rag.corpus import fetch as fetch_corpus
from rag.embedding_inputs import InputProfileError, vector_chunks
from rag.lexical import LexicalIndex, backend_info
from rag.ranking import documents as rank_documents
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

CJK = re.compile(r"[一-鿿]")
GRAM = re.compile(r"[一-鿿]+|[a-zA-Z0-9_\.]+")

BM25_K1 = 1.2
BM25_B = 0.75

REFRESH_TTL = 600.0  # 10 分钟懒刷新

# Keep the first three document candidates; prune only the low-scoring tail.
_CLIFF_RATIO = 0.25
_CLIFF_MIN_KEEP = 3

# Query-only function-word removal leaves corpus token statistics unchanged.
_QUERY_STOPWORDS = ("有没有", "怎么样", "为什么", "怎样", "怎么", "什么", "如何",
                    "哪些", "哪个", "哪里", "哪儿", "多少", "为何", "干啥", "干嘛",
                    "是否", "咋", "啥")


def _clean_query(query: str) -> str:
    for w in _QUERY_STOPWORDS:
        query = query.replace(w, "")
    return query


# Reviewed query variants contribute only tokens present in the current vocabulary.
_QUERY_SYNONYMS: tuple[dict, ...] = (
    {"key": "测评", "val": "评测",
     "why": "措辞漂移：1.26 回流用例「RAG测评体系怎么建立」期望 note:19，而那篇的小节标题"
            "写的是「评测」。语料里 测评 df=0、评测 df=2 ⇒ 补等价词，不改期望"},
    {"key": "令牌", "val": "JWT",
     "why": "访客说中文、语料写缩写：令牌全站 df=0（一次都没出现），JWT 见于 note:14/19/22。"
            "短问「令牌怎么签发？」扩展前有效 token=0 ⇒ 返回空；扩展后 rank=1"},
    {"key": "断线", "val": "断连",
     "why": "同上：断线 df=0、断连见于 note:19（断连中断机制那一节）。「断线了会怎样？」"
            "扩展前只靠一个功能词命中无关文档，扩展后 rank=1"},
    {"key": "空中升级", "val": "OTA",
     "why": "同义变体，但 升级 本身在语料里 ⇒ 收益弱于上面两对：「空中升级怎么做的？」"
            "扩展前后 rank 都是 1，**只把 note:12 拉进 @3**。留它是因为零回归，"
            "不是因为它是主收益——这是个「过了闸门但收益很小」的样本，别当成常态"},
)


def synonyms() -> tuple[dict, ...]:
    """词表的只读视图（判据与评测读它，不另抄一份）。"""
    return _QUERY_SYNONYMS


# Evaluation may compare expansion on/off; production uses the enabled setting.
_EXPANSION_ENABLED = True


def set_expansion(on: bool) -> None:
    """开/关查询侧同义扩展。**仅 A/B 与判据使用**，生产不调用。"""
    global _EXPANSION_ENABLED
    _EXPANSION_ENABLED = bool(on)


def expansion_enabled() -> bool:
    return _EXPANSION_ENABLED


def cliff_config() -> dict:
    """断崖参数的只读视图（α 与保底 K）。报告与判据读这里，不另抄一份数
    ——抄一份的那个数会漂，而这两项正是"判据与实现同源"要保的东西。"""
    return {"ratio": _CLIFF_RATIO, "min_keep": _CLIFF_MIN_KEEP}


def tokenize(text: str) -> list[str]:
    """中文连续段拆 2-gram/3-gram（子串匹配近似）+ 英文/数字按词。与 eval 一致。"""
    toks: list[str] = []
    for m in GRAM.finditer(text.lower()):
        t = m.group(0)
        if CJK.match(t):
            for n in (3, 2):
                toks.extend(t[i:i + n] for i in range(len(t) - n + 1))
        else:
            toks.append(t)
    return toks


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
    """**本线程**最近一次 `search()` 实际走的路线（默认 = 词法）。

    为什么记而不是"再判一次"：退回词法发生在函数内部（视图与语料对不上、查询嵌不出来），
    从返回值看不出来——而"开关开着、其实一次都没融合过"恰恰是最需要被看见的那一格。
    用线程局部：4 个 worker × 线程池，同一进程里并发的两条检索不能互相覆盖。
    """
    return dict(getattr(_route, "last",
                        {"mode": "lexical", "reason": None, "vectors": 0, "missing": 0}))


def _mode_for(reason: str | None) -> str:
    """没融合时的档位名：**跑不起来** = degraded，**没跑**或**跑了但没相关项** = lexical。

    区分点是有没有"出了事"：关着开关、向量路一条都没找到，都不是故障（`/health` 与
    工具 meta 里把它们报成 degraded 会让真正的故障——凭据没配、视图对不上——淹掉）。
    """
    return "lexical" if reason in (None, "no_vector_hits") else "degraded"


def _rrf_k() -> int:
    """RRF 的 k（标准 60）。取内存取值、给个兜底：`0`/缺项都不该把融合变成除零。"""
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
        self._last_attempt = 0.0
        self._last_error: str | None = None
        self._revision = ""
        self._dirty = False
        self._invalidations = 0
        self._docs: list[dict] = []
        self._chunks: list[dict] = []      # {doc_idx, type, id, title, section, text}
        self._vector_chunk_cache: tuple | None = None
        self._lexical: LexicalIndex | None = None
        self._last_build = 0.0

    # ── 构建 ──────────────────────────────────────────────────────

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

    def _build(self) -> None:
        invalidations = self._invalidations
        docs = self._corpus_loader() if self._corpus_loader else self._fetch_corpus()
        chunks: list[dict] = []
        for d in docs:
            for c in chunk_note(d["title"], d["content"]):
                chunks.append({**d, **c})

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
        from tools.base import _get
        return fetch_corpus(_get)

    def status(self) -> dict:
        with self._lock:
            return {"revision": self._revision, "documents": len(self._docs),
                    "stale": bool(self._last_error) or self.is_stale(),
                    "error": self._last_error, "last_success": self._last_build,
                    "lexical_backend": backend_info()}

    def docs_snapshot(self) -> list[dict]:
        """语料快照（[{type,id,title,content}]，浅拷贝列表）。索引没建好时为空列表。"""
        with self._lock:
            return list(self._docs)

    def vocabulary(self) -> frozenset[str]:
        """Read the vocabulary of the currently published lexical snapshot."""
        with self._lock:
            return self._lexical.vocabulary if self._lexical is not None else frozenset()

    def is_stale(self) -> bool:
        """没建过、或超过 REFRESH_TTL 未重建。"""
        return self._dirty or not self._last_build or time.time() - self._last_build > REFRESH_TTL

    # ── 查询 ──────────────────────────────────────────────────────

    def _lexical_ranked(self, query: str, top_k: int,
                        apply_cliff: bool = True) -> list[dict] | None:
        """Rank the complete lexical snapshot; None means unavailable, [] means no hits.

        apply_cliff=False is reserved for evaluation of candidate pruning.
        """
        if self.is_stale() and time.time() - self._last_attempt > 30:
            try:
                self.build()
            except Exception:
                logger.warning("Corpus refresh failed; retaining the last complete snapshot")
        with self._lock:
            if not self._last_build or time.time() - self._last_build > REFRESH_TTL * 2:
                return None
            if not self._chunks:
                return []
            chunks, lexical, revision = self._chunks, self._lexical, self._revision
        assert lexical is not None
        vocabulary = lexical.vocabulary
        # Deduplicate query terms without changing corpus frequencies.
        q_toks = list(dict.fromkeys(t for t in tokenize(_clean_query(query)) if t in vocabulary))
        if _EXPANSION_ENABLED:
            for _pair in _QUERY_SYNONYMS:
                if _pair["key"] in query:
                    q_toks += [
                        t for t in tokenize(_clean_query(query.replace(_pair["key"], _pair["val"])))
                        if t not in q_toks and t in vocabulary]
        q_toks = list(dict.fromkeys(q_toks))
        if not q_toks:
            return []
        scores = lexical.scores(q_toks)
        # Document score is its strongest matching chunk; retain section locations.
        ranked = rank_documents(chunks, scores, revision)
        # A relative cutoff preserves the first three candidates independently of score scale.
        if ranked and apply_cliff:
            floor = ranked[0]["score"] * _CLIFF_RATIO
            ranked = ranked[:_CLIFF_MIN_KEEP] + [
                r for r in ranked[_CLIFF_MIN_KEEP:] if r["score"] >= floor]
        ranked = ranked[:max(1, top_k)]
        for r in ranked:
            r["score"] = round(r["score"], 4)
            r["sections"] = r["sections"][:2]
        return ranked

    # ── 编排──────────────────────────

    def search(self, query: str, top_k: int = 8,
               apply_cliff: bool = True) -> list[dict] | None:
        """检索入口（编排器）：词法路恒跑；向量路在场且**与这批语料对齐**时才融合。

        `apply_cliff` 透传给词法路（见 `_lexical_ranked`）：**只给两臂 A/B 用**，生产恒默认。
        注意它只关**词法路**的断崖——向量路本来就没有断崖（`_vector_ranked` 只切 top_k），
        所以拨到 False 时"断崖前"这个口径只在词法档下严格成立（融合档下两臂本就该几乎相同）。

        三条硬规则（都不是风格问题，各自对应一种静默的错法）：

        1. **只有两路都在场才融合**。单路 RRF 与 BM25 不是同一套分数语义（≈0.016 vs
           个位数），跨查询或跨模式不能直接比较原始分数。降级保持词法结果的形状：
           关开关 ≡ 向量不可用 ≡ 融合前，
           逐字节同今天（含 BM25 原分、原断崖、原候选数）。
        2. **不在 RRF 分数空间里再造断崖**。每路各自剪枝（词法路保留 `_CLIFF_RATIO`），
           RRF 只对并集重排序；把 0.25 照搬到量级 ≈0.016 的融合分上等于把候选清空。
        3. **向量侧绝不进 `build()`**（见那里的注释），也绝不拿**对不上语料**的向量融合
           ——对齐由 `view_for(指纹)` 保证，对不上就是没有，宁可少一路。
        """
        lex = self._lexical_ranked(query, top_k, apply_cliff)
        if lex is None:
            _record("unavailable", None)
            return None
        vec, reason, miss = self._vector_ranked(query, top_k)
        if not vec:
            # 关着是"设定"、向量路跑了但没相关项是"正常"、跑不起来才是"降级"——
            # 三件事在下游眼里必须分得开，否则"开关拨了但一次都没生效"永远看不见。
            _record(_mode_for(reason), reason, missing=miss)
            return lex
        if lex and lex[0].get("corpus_revision") != vec[0].get("corpus_revision"):
            _record("degraded", "corpus_changed", missing=miss)
            return lex
        _record("hybrid", None, len(vec), miss)
        return rrf_fuse(lex, vec, _rrf_k(), top_k) or lex

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

    def _vector_ranked(self, query: str, top_k: int) -> tuple[list[dict], str | None, int]:
        """向量路候选（文档级取最高分，与词法路同构）。返回 (rows, 没融合的原因, 缺几条)。

        rows 为空 ⇒ 调用方整条退回词法。**它在"取不到"这件事上必须是诚实的**：
        没有向量就说没有（reason 报出去），绝不返回"上一批语料"或"另一个空间"的向量。
        reason = `no_vector_hits` 是"跑了、但这段语料里没有相关项"，不是故障。
        """
        if not is_enabled():
            # 开关关着、或凭据没配全（`degraded_reason()` 分得清这两件事，见它自己的注）
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
        store.load()        # 读一次 manifest（几百字节，签名没变就早退）：不读的话，本进程
                            # **第一次**检索必然报"还没有向量"——盘上明明是好的。读它顺带也
                            # 就是"别的 worker 刚更新过 ⇒ 这里换视图"的那一步。
        view = store.view_for(corpus_fingerprint(space, chunks))
        if view is None:
            self._warm_vectors(chunks)    # 后台补建/补齐；本轮不阻塞、不等它
            return [], degraded_reason() or "no_vectors", len(store.missing())
        if len(view) != len(chunks):    # 不变式②在装载时已验，这里防"指纹撞上别的语料"
            logger.warning("向量视图 %d 行 != 语料 %d chunk——本轮退回纯词法",
                           len(view), len(chunks))
            return [], "stale_view", len(store.missing())
        qv = embed_query(query)
        if qv is None:
            return [], "query_embed_failed", len(store.missing())
        sims = view.similar(qv)
        ranked = rank_documents(chunks, {i:s for i, s in enumerate(sims) if s > 0},
                                revision)[:max(1, top_k)]
        for r in ranked:
            r["score"] = round(r["score"], 4)
            r["sections"] = r["sections"][:2]
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
        if not any(d["id"] == article_id and d.get("revision") == revision for d in idx._docs):
            return
        idx._dirty = True
        idx._invalidations += 1
        idx._last_attempt = 0.0
    warm_async()


def search(query: str, top_k: int = 8, apply_cliff: bool = True) -> list[dict] | None:
    """检索入口：返回候选列表 [{type, id, title, section, score}]，不含全文。

    `apply_cliff=False` 关掉词法路的相对断崖（`eval/cliff_probe.py` 的两臂用），**线上恒默认**。
    """
    return get_index().search(query, top_k=top_k, apply_cliff=apply_cliff)


# Resolve only unambiguous article-title references from the visible corpus.
# No match or multiple matches return None rather than guessing an article ID.

_WS_RE = re.compile(r"\s+")
_TITLE_PART_MIN = 4  # 简称子串匹配的最短长度（《架构文档》可以，两字标题不行）

# 文章 id 形态由接口给定（noteKey 现为整数，类型不写死——回执/锚点都按字符串用）
DocId = int | str


def _norm_title(s: str) -> str:
    """标题归一化：去空白 + ASCII 折小写（中文不受影响）。"""
    return _WS_RE.sub("", s or "").lower()


def match_doc_title(title: str, docs: list[dict]) -> DocId | None:
    """标题 → 文章 id（纯函数，便于单测）。唯一命中才返回 id，否则 None。

    ① 归一化后完全相等；
    ② 唯一子串包含（口语简称《架构文档》对全称《…架构文档》），两侧任一方向且
       锚点标题 ≥_TITLE_PART_MIN 字。两条都要求**候选唯一**——两条候选打平就是
       歧义，返回 None。
    """
    n = _norm_title(title)
    if not n or not docs:
        return None
    titles = [(d, _norm_title(d.get("title", ""))) for d in docs]
    exact = [d for d, t in titles if t == n]
    if len(exact) == 1:
        return exact[0]["id"]
    if len(exact) > 1:          # 同名文章（站内允许）：歧义，不猜
        return None
    if len(n) < _TITLE_PART_MIN:
        return None
    part = [d for d, t in titles if n in t or t in n]
    if len(part) == 1:
        return part[0]["id"]
    return None


def resolve_title(title: str) -> DocId | None:
    """按标题查文章 id；索引没建好返回 None。**不阻塞**调用方（只读快照）。

    冷/过期时踢一次后台重建（不在本调用里等）——下一次解析就能拿到新文章。
    """
    idx = get_index()
    docs = idx.docs_snapshot()
    if idx.is_stale():
        warm_async()
    return match_doc_title(title, docs)


def warm() -> bool:
    """同步建好语料索引（启动预热、评测夹具、单测用）。失败返回 False 不抛。"""
    try:
        return get_index().build()
    except Exception:
        logger.warning("语料预热失败：检索与锚点标题解析都会降级到旧/空索引",
                       exc_info=True)
        return False


_warm_lock = threading.Lock()   # 只防同刻重复起线程，不阻塞调用方


def warm_async() -> None:
    """后台预热一次（不等待）。并发调用只会起一个线程，重建幂等。"""
    if not _warm_lock.acquire(blocking=False):
        return
    def _run() -> None:
        try:
            warm()
        finally:
            _warm_lock.release()
    threading.Thread(target=_run, name="rag-warm", daemon=True).start()



if __name__ == "__main__":
    import sys
    idx = get_index()
    idx.build()
    q = sys.argv[1] if len(sys.argv) > 1 else "ESP32-S3 OTA 更新需要哪些分区？"
    for h in idx.search(q) or []:
        print(f"  {h['type']}:{h['id']:<4} {h['score']:<8.4f} [{h['section']}] {h['title'][:24]}")
