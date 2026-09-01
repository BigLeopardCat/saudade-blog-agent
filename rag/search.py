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

import math
import re
import threading
import time

from tools.base import _client

CJK = re.compile(r"[一-鿿]")
GRAM = re.compile(r"[一-鿿]+|[a-zA-Z0-9_\.]+")

REFRESH_TTL = 600.0  # 10 分钟懒刷新

# 语料源：Rust 公开接口（与前台可见性严格一致，agent 保持无 DB 依赖架构）
#  - notes:    is_public=1 AND status!='draft'（前台可读的文章）
# 20260901 语料净化：检索池只收文章——说说（碎碎念）语义判别力低、留言混杂，
# 混入检索池只会放大无关候选 → 幻觉空间（"最新留言"事故实证：rag_search 返回
# 混合候选，模型在 talk 候选上硬编 talkKey 31）。说说/留言/公告是"数据读取"
# 场景，走各自数据工具（list_talks/list_guestbook/get_announcements），不进检索池。


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
        # 列表接口不返回正文（noteContent 为空），须逐篇拉全文
        for it in _get("/notes"):
            detail = _get(f"/notes/{it.get('noteKey')}") or {}
            docs.append({"type": "note", "id": it.get("noteKey"),
                         "title": it.get("noteTitle") or "",
                         "content": detail.get("noteContent") or ""})
        # 20260901：语料只收文章（说说/留言/公告移除——检索池净化，见头部注释）
        return docs

    # ── 查询 ──────────────────────────────────────────────────────

    def search(self, query: str, top_k: int = 8) -> list[dict]:
        if time.time() - self._last_build > REFRESH_TTL:
            try:
                self.build()
            except Exception:
                pass  # 重建失败用旧索引（语料拉取失败不该让检索崩溃）
        with self._lock:
            chunks, doc_tf, postings, idf, avgdl = (
                self._chunks, self._doc_tf, self._postings, self._idf, self._avgdl)
        q_toks = [t for t in tokenize(query) if t in postings]
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


if __name__ == "__main__":
    import sys
    idx = get_index()
    idx.build()
    q = sys.argv[1] if len(sys.argv) > 1 else "ESP32-S3 OTA 更新需要哪些分区？"
    for h in idx.search(q):
        print(f"  {h['type']}:{h['id']:<4} {h['score']:<8.4f} [{h['section']}] {h['title'][:24]}")
