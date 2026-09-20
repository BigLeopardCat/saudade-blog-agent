"""RAG 检索管线（路线 B：检索只定位，解读走 get_article_detail 全文）。

20260830 修复：get_article_detail 泛化 doc_type（note/talk/board/announcement 均可读全文），
talk/board/announcement 无单条端点，从列表接口按 key 过滤（列表已带全文）——见 tools/base.py。

设计（2026-08-30 边界定论）：
- 检索粒度 = 小段落 chunk（markdown 标题切分，>2000 字才切）；读取粒度 = 全文。
- 语料 = 线上可见文章（is_public=1 AND status!='draft'）——20260901 起检索池
  只收文章（说说/留言/公告移除：碎碎念与混杂内容无语义判别力，混入放大幻觉，
  见"最新留言"事故；它们走 list_talks/list_guestbook/get_announcements 数据工具）。
  与前台可见性严格一致——agent 不应答出访客看不到的内容。
- 词法 2/3-gram 子串匹配（recall_eval 实证：14 条 recall 用例 recall@3=1.00，
  词法基线已打满当前语料；向量留作 L1 升级，接 BEIR 基准时对比再上）。
- 存储 = 内存倒排（语料 34 文档，全量重建 <100ms，不做增量）；懒刷新（10 分钟 TTL）。
- 返回候选 (type, id, title, section, score) 列表，不返回全文（路线 B 契约）。

eval/recall_eval.py 直接 import 本模块的 search()——评测即线上实现。
"""
from __future__ import annotations

import logging
import math
import re
import threading
import time

from tools.base import _client

logger = logging.getLogger(__name__)

CJK = re.compile(r"[一-鿿]")
GRAM = re.compile(r"[一-鿿]+|[a-zA-Z0-9_\.]+")

REFRESH_TTL = 600.0  # 10 分钟懒刷新

# 语料拉取的翻页参数。见 _fetch_corpus 的说明：不传 page_size 会吃服务端默认 6 篇。
CORPUS_PAGE_SIZE = 50
CORPUS_PAGE_MAX = 40  # 兜底上限（50×40 = 2000 篇），防服务端异常时无限翻页

# 语料源：Rust 公开接口（与前台可见性严格一致，agent 保持无 DB 依赖架构）
#  - notes:    is_public=1 AND status!='draft'（前台可读的文章）
# 20260901 语料净化：检索池只收文章——说说（碎碎念）语义判别力低、留言混杂，
# 混入检索池只会放大无关候选 → 幻觉空间（"最新留言"事故实证：rag_search 返回
# 混合候选，模型在 talk 候选上硬编 talkKey 31）。说说/留言/公告是"数据读取"
# 场景，走各自数据工具（list_talks/list_guestbook/get_announcements），不进检索池。

# ── 查询侧处理（只动查询串，索引零影响；加词纪律：每加一词须全量 recall_eval
#    21 query 复验无回归才可留）──

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


# 同义扩展（20260905，同日同案）：词法 2/3-gram 下 2 字同义/惯用变体零共享 gram
# ——访客问「测评体系」与文档用词「评测」互不可见（实测 query gram 只剩体系/怎么
# 命中）。替换出同义变体的新 gram 并入 query token 集（与原文 gram 去重后各自
# 权重 1）。只收「替换后语义不变」的词对。
_QUERY_SYNONYMS = {
    "测评": "评测",  # 1.26 P0 回流用例「RAG测评体系怎么建立」期望 note:19（架构文档
                     # 「# 评测：eval/golden…」小节措辞为「评测」）——1.27 事故族：
                     # 语料改写措辞漂移使期望失配，检索侧用等价词对补上而不是改期望
}


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
    """markdown 标题切 chunk；短文（<2000 字符）不切。返回 [{section, text}]。"""
    if len(content) < 2000:
        return [{"section": title, "text": content}]
    chunks, cur, cur_section = [], [], title
    for line in content.split("\n"):
        m = re.match(r"^#{1,3}\s+(.+)$", line.strip())
        if m:
            if cur:
                chunks.append({"section": cur_section, "text": "\n".join(cur)})
            cur_section, cur = m.group(1), []
        else:
            cur.append(line)
    if cur:
        chunks.append({"section": cur_section, "text": "\n".join(cur)})
    return chunks


class RagIndex:
    """内存倒排索引 + 懒刷新。全量重建幂等，线程安全（重建后原子替换）。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._docs: list[dict] = []
        self._chunks: list[dict] = []      # {doc_idx, type, id, title, section, text}
        self._postings: dict[str, list[int]] = {}  # gram -> [chunk_idx]
        self._doc_tf: list[dict[str, int]] = []    # per-chunk term freq
        self._avgdl = 0.0
        self._idf: dict[str, float] = {}
        self._last_build = 0.0

    # ── 构建 ──────────────────────────────────────────────────────

    def build(self) -> None:
        docs = self._fetch_corpus()
        chunks: list[dict] = []
        for d in docs:
            for c in chunk_note(d["title"], d["content"]):
                chunks.append({**d, "section": c["section"], "text": c["text"]})

        doc_tf: list[dict[str, int]] = []
        postings: dict[str, list[int]] = {}
        for ci, c in enumerate(chunks):
            tf: dict[str, int] = {}
            for t in tokenize(c["title"] + "\n" + c["text"]):
                tf[t] = tf.get(t, 0) + 1
            doc_tf.append(tf)
            for t in tf:
                postings.setdefault(t, []).append(ci)

        n = len(chunks)
        avgdl = sum(len(t) for t in doc_tf) / max(n, 1)
        df = {t: len(v) for t, v in postings.items()}
        idf = {t: math.log(1 + (n - f + 0.5) / (f + 0.5)) for t, f in df.items()}

        with self._lock:
            self._docs, self._chunks = docs, chunks
            self._doc_tf, self._postings = doc_tf, postings
            self._avgdl, self._idf = avgdl, idf
            self._last_build = time.time()

    def _fetch_corpus(self) -> list[dict]:
        from tools.base import _get  # _get 已含 API_BASE 前缀 + code==200 校验

        docs: list[dict] = []
        # 列表接口不返回正文（noteContent 为空），须逐篇拉全文。
        # ⚠️ 必须显式翻页：`/notes` 不传 page_size 时吃服务端默认 6
        # （src/routes/notes.rs `page_size.unwrap_or(6)`，page 默认 1）。20260830 起语料
        # 实际只覆盖 6 篇——收了垃圾文 `13 TEST8`，却漏掉真文章
        # `12 ESP32-S3 OTA 问题与解决记录`（同为公开文章，建图脚本走 pageSize=1000 不受影响）。
        # 不硬编一个大 page_size：服务端 clamp(1,1000)，文章数早晚会越过它；
        # 翻到"不足一页"为止，顺带用 seen 去重（防服务端排序抖动导致跨页重复）。
        page = 1
        seen: set = set()
        while page <= CORPUS_PAGE_MAX:
            batch = _get(f"/notes?page={page}&page_size={CORPUS_PAGE_SIZE}")
            if not isinstance(batch, list) or not batch:
                break
            for it in batch:
                key = it.get("noteKey")
                if key is None or key in seen:
                    continue
                seen.add(key)
                detail = _get(f"/notes/{key}") or {}
                docs.append({"type": "note", "id": key,
                             "title": it.get("noteTitle") or "",
                             "content": detail.get("noteContent") or ""})
            if len(batch) < CORPUS_PAGE_SIZE:
                break
            page += 1
        if len(seen) >= CORPUS_PAGE_SIZE * CORPUS_PAGE_MAX:
            logger.warning("语料翻页到达上限 %d 页，可能仍有未收录文章", CORPUS_PAGE_MAX)
        logger.info("语料构建：%d 篇（翻页 %d 页）", len(docs), page)
        # 20260901：语料只收文章（说说/留言/公告移除——检索池净化，见头部注释）
        return docs

    def docs_snapshot(self) -> list[dict]:
        """语料快照（[{type,id,title,content}]，浅拷贝列表）。索引没建好时为空列表。"""
        with self._lock:
            return list(self._docs)

    def is_stale(self) -> bool:
        """没建过、或超过 REFRESH_TTL 未重建。"""
        return not self._docs or time.time() - self._last_build > REFRESH_TTL

    # ── 查询 ──────────────────────────────────────────────────────

    def search(self, query: str, top_k: int = 8) -> list[dict] | None:
        """返回候选列表；**索引不可用时返回 None**（区别于「没命中」的 []）。

        20260917：此前两种失败都返回 []，工具层只能一律包成 empty(「检索无结果」)——
        语料拉取失败会被 checker 记成「检索过、确实没有」的**事实**（外部审计指出）。
        注意**不能**用「事先问一句 ready()」来判：索引是懒建的，冷启动时 chunks 为空
        但完全可用，那样会误伤第一次检索。所以判据放在这里：建完之后还是空的才算不可用。
        """
        if time.time() - self._last_build > REFRESH_TTL:
            try:
                self.build()
            except Exception:
                pass  # 重建失败用旧索引（语料拉取失败不该让检索崩溃）
        with self._lock:
            if not self._chunks:
                # 建过（或刚试过）但语料仍为空 ⇒ 索引不可用，不是「没命中」
                return None
            chunks, doc_tf, postings, idf, avgdl = (
                self._chunks, self._doc_tf, self._postings, self._idf, self._avgdl)
        # 疑问词先剔除（句法功能词非内容；不做会稀释实词权重，实证见 _QUERY_STOPWORDS），
        # 同义扩展后仍经同一剔除路径（否则 怎么 会经替换串溜回，见 20260905 模拟）
        q_toks = [t for t in tokenize(_clean_query(query)) if t in postings]
        for key, val in _QUERY_SYNONYMS.items():
            if key in query:
                q_toks += [t for t in tokenize(_clean_query(query.replace(key, val)))
                           if t not in q_toks and t in postings]
        if not q_toks:
            return []
        # chunk 级 BM25 打分
        scores: dict[int, float] = {}
        for t in q_toks:
            w = idf[t]
            for ci in postings[t]:
                dl = sum(doc_tf[ci].values())
                tf = doc_tf[ci].get(t, 0)
                scores[ci] = scores.get(ci, 0.0) + w * (tf * 1.2) / (
                    tf + 1.2 * (1 - 0.75 + 0.75 * dl / max(avgdl, 1)))
        # 文档级聚合：候选 = 文档（路线 B 契约：解读走 get_article_detail 全文），
        # 分数取该文档命中 chunk 的最高分，sections 汇总命中小节供定位
        by_doc: dict[tuple[str, int], dict] = {}
        for ci, score in sorted(scores.items(), key=lambda x: -x[1]):
            c = chunks[ci]
            key = (c["type"], c["id"])
            agg = by_doc.get(key)
            if agg is None:
                by_doc[key] = {"type": c["type"], "id": c["id"],
                               "title": c["title"], "score": score,
                               "sections": [c["section"]]}
            elif score > agg["score"]:
                agg["score"], agg["sections"] = score, [c["section"]]
            elif c["section"] not in agg["sections"]:
                agg["sections"].append(c["section"])
        ranked = sorted(by_doc.values(), key=lambda x: -x["score"])[:top_k]
        for r in ranked:
            r["score"] = round(r["score"], 4)
        return ranked


_index: RagIndex | None = None


def get_index() -> RagIndex:
    global _index
    if _index is None:
        _index = RagIndex()
    return _index


def search(query: str, top_k: int = 8) -> list[dict]:
    """检索入口：返回候选列表 [{type, id, title, section, score}]，不含全文。"""
    return get_index().search(query, top_k=top_k)


# ── 标题 → id 确定性解析（20260920 方案①）────────────────────────────
# 动机：用户在会话里用《标题》点名一篇文档、而历史里从没出现过它的 id 时，planner
# 拿到的锚点只有"（未见过 id）"，只能去 list_notes 里猜下标——线上实测它把分页
# 列表的第一条（最新那篇）当成"用户点名的这篇"，读错文章、还谎称站内没有该文
# （真文存在）。语料索引里本来就有全部可见文章的标题与 id，直接解析即可，模型
# 不必猜。锚点是**确定性事实**：解析只认唯一命中，只要有一丝歧义就返回 None，
# 让上游继续显示"未见过 id"（宁可让模型按规则去查，绝不给错 id）。

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
        get_index().build()
        return True
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
