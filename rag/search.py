"""Article retrieval over complete, versioned public corpus snapshots.

Index text preserves heading paths, chunks have bounded source ranges, and BM25
uses total token counts consistently. Optional vector retrieval uses the same text.
Failures retain the last complete snapshot for a bounded grace period and expose
staleness; a failed page is never interpreted as an empty or complete corpus.
"""
from __future__ import annotations

import logging
import math
import re
import threading
import time
from collections.abc import Callable

from config.settings import settings
from rag.chunks import split as split_chunks
from rag.corpus import corpus_revision
from rag.corpus import fetch as fetch_corpus
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

# 改个名再进来：本模块**自己**有一个 `warm_async()`（后台重建**语料/BM25** 索引，见文件末），
# 同名会把它盖掉——而那个是被 `resolve_title()` 调的（标题→id 锚点），盖掉的后果是它变成
# 一个要参数的函数、一调就 TypeError。两件事都叫"预热"，但一个是重建词法索引、一个是补向量。
from rag.vector_index import warm_async as warm_vectors

logger = logging.getLogger(__name__)

CJK = re.compile(r"[一-鿿]")
GRAM = re.compile(r"[一-鿿]+|[a-zA-Z0-9_\.]+")

BM25_K1 = 1.2
BM25_B = 0.75

REFRESH_TTL = 600.0  # 10 分钟懒刷新

# ── 候选截断（20260920 批次 d 供给端，标定见模块头注释）──
_CLIFF_RATIO = 0.25   # 相对断崖：第 _CLIFF_MIN_KEEP 名之后、低于 top1×0.25 的候选丢弃
_CLIFF_MIN_KEEP = 3   # 保底：前 3 名永不参与断崖（20261011，理由见模块头注释"断崖保底"）

# 语料由版本化公开接口提供；服务端复用前台详情的可见性谓词。
# 20260901 语料净化：检索池只收文章——说说（碎碎念）语义判别力低、留言混杂，
# 混入检索池只会放大无关候选 → 幻觉空间（"最新留言"事故实证：rag_search 返回
# 混合候选，模型在 talk 候选上硬编 talkKey 31）。说说/留言/公告是"数据读取"
# 场景，走各自数据工具（list_talks/list_guestbook/get_announcements），不进检索池。

# ── 查询侧处理（只动查询串，索引零影响）──

# 疑问/句法功能词剔除（20260905 处置 rag_eval_system）：中文疑问句的信息在
# 实词上，「怎么/什么/哪些」等纯句法词 df 常极小（实测「怎么」df=2 → idf 4.633，
# 一个无关短 chunk 单命中 2.18 就能压过真答案 1.49——Git 教程/固件文档双双登顶）。
# 串级剔除（按长到短，防「怎么样/为什么」先被短词拆残）后实词照常打分；
# 只在查询侧做，索引与语料词频统计不受影响。
_QUERY_STOPWORDS = ("有没有", "怎么样", "为什么", "怎样", "怎么", "什么", "如何",
                    "哪些", "哪个", "哪里", "哪儿", "多少", "为何", "干啥", "干嘛",
                    "是否", "咋", "啥")


def _clean_query(query: str) -> str:
    for w in _QUERY_STOPWORDS:
        query = query.replace(w, "")
    return query


# 同义扩展（20260905 起，20261009 扩表并加闸门）：词法 2/3-gram 下，访客用的词与语料
# 用的词可以零共享 gram。替换出同义变体的新 gram 并入 query token 集（与原文 gram
# 去重后各自权重 1）。只收「替换后语义不变」的词对。
#
# 两类病因，机制是同一个：
#   A **措辞漂移**——语料改写过，期望没跟着改（测评/评测，1.27 事故族）；
#   B **站内根本没这个词**——访客说中文、语料写缩写或另一种叫法（令牌/JWT、断线/断连，
#     两词的 df 都是 0）。B 类比 A 类更狠：查询有效 token 掉到 0 ⇒ **检索直接返回空**，
#     不是排得不好，是一条候选都没有。
#
# **加词纪律 = 两道闸门，管的是不同问题，缺一不可**：
#   ① 机械闸门 `tests/test_query_expansion.py`：这一对**对不对**——val 在语料里真有落点、
#      且带去 key 到不了的 chunk（非空操作 + 有增量），并且**必须被标注集里的某条 query
#      真的用到**。没人用过的一对词是"看起来在治病的注释"，不是能力。
#   ② 经验闸门 `eval/recall_eval.py`：加了**到底好不好**——主集 22 条零回归、留出集有改善。
#      **别拿 ① 当放行标准**：①只拦"明显错/没用"，判不出收益大小（空中升级→OTA 就是
#      过了 ① 而收益只在 @3 上）。
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


# 扩展开关（**只给 A/B 与判据用，生产恒开**）。做成模块级而不是 `.env` 拨盘，是因为它
# 唯一的消费方是"同一进程里跑两臂"的评测：离线套件按出厂档跑、结构上只会跑一档
# （见 tests/conftest.py 里 `_PINNED` 上方那段说明），做成 .env 拨盘等于让判据永远看不见另一臂。
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


# ── 本轮实际走了哪条路（20261005）────────────────────────────────────
# 混合与纯词法的**出口文本一模一样**，事后从候选行里看不出来（RRF 分 ≈0.016、
# BM25 分是个位数，混在两套量级里也读不出"这轮融合过没有"）。所以由检索端**记下
# 事实**，工具出口把它当 meta 带出去（`tools/base.py::rag_search`）。
_route = threading.local()


def _record(mode: str, reason: str | None, vectors: int = 0, missing: int = 0) -> None:
    _route.last = {"mode": mode, "reason": reason,
                   "vectors": vectors, "missing": missing}


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
    """内存倒排索引 + 懒刷新。全量重建幂等，线程安全（重建后原子替换）。"""

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
        self._lengths: list[int] = []
        self._docs: list[dict] = []
        self._chunks: list[dict] = []      # {doc_idx, type, id, title, section, text}
        self._postings: dict[str, list[int]] = {}  # gram -> [chunk_idx]
        self._doc_tf: list[dict[str, int]] = []    # per-chunk term freq
        self._avgdl = 0.0
        self._idf: dict[str, float] = {}
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

        doc_tf: list[dict[str, int]] = []
        postings: dict[str, list[int]] = {}
        for ci, c in enumerate(chunks):
            tf: dict[str, int] = {}
            for t in tokenize(chunk_text(c["title"], c["text"])):
                tf[t] = tf.get(t, 0) + 1
            doc_tf.append(tf)
            for t in tf:
                postings.setdefault(t, []).append(ci)

        n = len(chunks)
        lengths = [sum(t.values()) for t in doc_tf]
        avgdl = sum(lengths) / max(n, 1)
        revision = corpus_revision(docs)
        df = {t: len(v) for t, v in postings.items()}
        idf = {t: math.log(1 + (n - f + 0.5) / (f + 0.5)) for t, f in df.items()}

        with self._lock:
            self._docs, self._chunks = docs, chunks
            self._doc_tf, self._postings = doc_tf, postings
            self._avgdl, self._idf = avgdl, idf
            self._lengths, self._revision = lengths, revision
            self._last_error = None
            self._dirty = self._invalidations != invalidations
            if self._dirty:
                self._last_attempt = 0.0
            self._last_build = time.time()
        # 向量侧**不进 build()**：这是懒刷新的热路径（TTL 一到就被评测/对话触发），
        # 本方法只同步拉取语料、计算词法索引，不等待远端 embedding。
        # 向量预热走后台；开关关着时不发 embedding 请求。
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
                    "error": self._last_error, "last_success": self._last_build}

    def docs_snapshot(self) -> list[dict]:
        """语料快照（[{type,id,title,content}]，浅拷贝列表）。索引没建好时为空列表。"""
        with self._lock:
            return list(self._docs)

    def is_stale(self) -> bool:
        """没建过、或超过 REFRESH_TTL 未重建。"""
        return self._dirty or not self._last_build or time.time() - self._last_build > REFRESH_TTL

    # ── 查询 ──────────────────────────────────────────────────────

    def _lexical_ranked(self, query: str, top_k: int,
                        apply_cliff: bool = True) -> list[dict] | None:
        """**词法路**（BM25）：恒跑，也是唯一的降级形态（20261005 起不叫 `search`）。

        返回候选列表；**索引不可用时返回 None**（区别于「没命中」的 []）。

        `apply_cliff=False` 只关断崖、其余逐字节相同（同一套打分、同一个 top_k、同一个
        扩展开关）——`eval/cliff_probe.py` 要的"有/无断崖两臂"靠它，**生产调用方不该传它**
        （默认 True 就是唯一的线上形态）。

        20260917：此前两种失败都返回 []，工具层只能一律包成 empty(「检索无结果」)——
        语料拉取失败会被 checker 记成「检索过、确实没有」的**事实**。
        完整快照为空是正常结果；尚未成功构建、或旧快照超过宽限期才算不可用。
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
            chunks, doc_tf, postings, idf, avgdl = (
                self._chunks, self._doc_tf, self._postings, self._idf, self._avgdl)
            lengths, revision = self._lengths, self._revision
        # 疑问词先剔除（句法功能词非内容；不做会稀释实词权重，实证见 _QUERY_STOPWORDS），
        # 同义扩展后仍经同一剔除路径（否则 怎么 会经替换串溜回，见 20260905 模拟）
        q_toks = list(dict.fromkeys(t for t in tokenize(_clean_query(query)) if t in postings))
        # 扩展只加「语料里真有的 gram」（`t in postings`）——val 若是个语料里不存在的词，
        # 这一对就是空操作（判据里用负控钉住：换一个空词进来，结果必须与关臂一致）。
        if _EXPANSION_ENABLED:
            for _pair in _QUERY_SYNONYMS:
                if _pair["key"] in query:
                    q_toks += [
                        t for t in tokenize(_clean_query(query.replace(_pair["key"], _pair["val"])))
                        if t not in q_toks and t in postings]
        q_toks = list(dict.fromkeys(q_toks))
        if not q_toks:
            return []
        # chunk 级 BM25 打分
        scores: dict[int, float] = {}
        for t in q_toks:
            w = idf[t]
            for ci in postings[t]:
                dl = lengths[ci]
                tf = doc_tf[ci].get(t, 0)
                scores[ci] = scores.get(ci, 0.0) + w * (tf * (BM25_K1 + 1)) / (
                    tf + BM25_K1 * (1 - BM25_B + BM25_B * dl / max(avgdl, 1)))
        # 文档级聚合：候选 = 文档（路线 B 契约：解读走 get_article_detail 正文范围），
        # 分数取该文档命中 chunk 的最高分，sections 汇总命中小节供定位
        ranked = rank_documents(chunks, scores, revision)
        # 相对断崖（20260920 批次 d，标定见模块头注释）：**第 _CLIFF_MIN_KEEP 名之后**、
        # 低于 top1×α 的候选丢弃。尺度无关是硬要求——绝对噪声下限已实证无可用阈值
        # （20260916d/e 弃权闸），勿复引入。
        # 保底 top-K（20261011）：`ranked[:_K]` 原样保留（**不过滤**），floor 只作用在尾巴上。
        # 这一行就是"断崖不可能改变 recall@1/@3"的**唯一实现点**——别把它写回整列过滤
        # （那样 floor 会跟着"语料里最强的文档"漂，同主题的真答案被自己切掉，见模块头注释）。
        if ranked and apply_cliff:
            floor = ranked[0]["score"] * _CLIFF_RATIO
            ranked = ranked[:_CLIFF_MIN_KEEP] + [
                r for r in ranked[_CLIFF_MIN_KEEP:] if r["score"] >= floor]
        ranked = ranked[:max(1, top_k)]
        for r in ranked:
            r["score"] = round(r["score"], 4)
            r["sections"] = r["sections"][:2]
        return ranked

    # ── 编排（20261005：词法 + 向量两路）──────────────────────────

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
            chunks = list(self._chunks)
            revision = self._revision
        store = get_store()
        store.load()        # 读一次 manifest（几百字节，签名没变就早退）：不读的话，本进程
                            # **第一次**检索必然报"还没有向量"——盘上明明是好的。读它顺带也
                            # 就是"别的 worker 刚更新过 ⇒ 这里换视图"的那一步。
        view = store.view_for(corpus_fingerprint(space_from_settings(), chunks))
        if view is None:
            warm_vectors(chunks)          # 后台补建/补齐；本轮不阻塞、不等它
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


# ── 标题 → id 确定性解析（20260920 方案①）────────────────────────────
# 动机：用户在会话里用《标题》点名一篇文档、而历史里从没出现过它的 id 时，planner
# 拿到的锚点只有"（未见过 id）"，只能去 list_notes 里猜下标——线上实测它把分页
# 列表的第一条（最新那篇）当成"用户点名的这篇"，读错文章、还谎称站内没有该文
# （真文存在）。语料索引里本来就有全部可见文章的标题与 id，直接解析即可，模型
# 不必猜。锚点是**确定性事实**：解析只认唯一命中，只要有一丝歧义就返回 None
# （宁可让模型按规则去查，绝不给错 id）。**20260925 起**上游拿到 None 的处理从
# "写（未见过 id）留在清单里"改成"这条不进清单"——留着的代价是清单里混进站内
# 通知/公告的标题（抬头声称的却是"已点名文档"，见 agent/prompt_context.py:_doc_anchors）。

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
    for h in idx.search(q):
        print(f"  {h['type']}:{h['id']:<4} {h['score']:<8.4f} [{h['section']}] {h['title'][:24]}")
