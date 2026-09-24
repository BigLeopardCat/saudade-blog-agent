#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""语料术语派生（20260924）：gold **声明来源文档**，断言词由语料**运行期派生**。

要治的病（`rag_ota_http` 现场）：那条用例的 gold 词是人手抄的 `esp_https_ota` /
`轮询` / `A/B`——实测前两个 df=2（落在 note 14、22），`A/B` 被 `tokenize` 按字符类
切碎（`/` 不在 `GRAM` 里）只剩 `a`/`b` 各自 df=6（满语料，恒真），而检索首位是
**note 12**（三个词一个都不含）。于是"期望"与"实际语料"各说各话，每次语料改写都要
人去改词表，改完还是错。

新判据：gold 只写"这篇回答必须扎根在 note:14 / note:22 这两篇里"，术语由**语料本身**
派生（本模块）。不是循环论证——术语来自 gold 里写死的**语料事实**（那两篇的 id），
正文来自 LLM；"回复必须扎根于检索结果"那种写法才是循环（检索结果是模型自己的产物）。

**为什么不并进 `corpus_check.py`**：那个模块在启动路径上做**文档级在位性**（expected
是否还在语料里），跑一次验一批；本模块是**按需逐用例**的术语派生，且要吃任意文档集合
（含内联夹具），不硬调 `idx.build()`（离线可测）。

过滤规则（每条都有实测依据，不许扩成"人抄的词表"）：
  S1 长度 ≥ 2（单字符 token 做断言词等于恒真）；
  S2 **没有字母也没有汉字的 token 一律排除**（纯数字、纯标点、`1.`、`1.1.0`、`..`）——
     理由是**形态学**：数字在语料里是阈值/端口/版本号，不是主题词，且跨篇偶然同形
     （实测 `8010` 落在 13/19、`30` 落在 19/46）；点号串是 markdown 序号/省略号的残渣。
     **不许**把它写成"它们不在正文里"——它们在，只是不承担主题；
  S3 停用词/疑问词（复用检索侧那一份 `_QUERY_STOPWORDS`，单一事实源）+ 中文功能词；
  S4 **df 门**：按**篇**计（同一篇里出现多次只算 1），默认 `df <= df_max`，
     `strict=True` ⇒ `df == 1`。门是相对**整个语料**判的、候选集是相对**申报文档**取的
     ——两者缺一，派生集要么恒真（无 df 门）要么冒充专有（无申报集）；
  S5 非词形**只标记不排除**：含 `.` 标 `id_like`、纯 CJK 3-gram 标 `gram3`——
     中文 token 是 2/3-gram 不是词，人可读性必须靠回显（`--show`）而不是靠删。

排序：含 ASCII 字母优先 → 其中**不含 `.` 的再优先**（`esp_https_ota` 这类形态最像专有术语；
实测 11 篇语料下若把含点号的长串排在前，`cap=40` 会被 MQTT 配置路径占满、主题词一个都浮不上来）
→ df 升 → 长度降 → 词频降 → 字典序（末位为稳定输出，否则 `cap` 截谁看集遍历顺序）。
**`cap` 只服务于回显，判据侧用 `cap=None`**（截断按 df 升序取到的是最冷僻的 df=1 标识符，
会把手边素材正确、只是用了常见词的诚实回答判红——理由详见 `derive` 的 docstring）。

用法：
  .venv/bin/python eval/corpus_terms.py --show note:14,note:22   # 逐篇派生（需网络）
  .venv/bin/python eval/corpus_terms.py --show note:12 --df-max 4
  .venv/bin/python eval/corpus_terms.py --drift                  # 漂移哨兵（扫 golden 词表）
  .venv/bin/python eval/corpus_terms.py --drift --case rag_ota_http
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from rag.search import _QUERY_STOPWORDS, tokenize  # noqa: E402

# S2：**没有字母也没有汉字的 token 一律排除**（纯数字、纯标点、`1.`、`1.1.0`、`..`）。
# 形态学判据，不是"内容判据"：数字在语料里是阈值/端口/版本号，不是主题词（实测 `8010`
# 落在 13/19、`30` 落在 19/46，跨篇偶然同形）；点号串是 markdown 序号/省略号的残渣
# （实测 note:14∩note:22 的派生集里混着 `..`、`...`、`1.`、`1.1.0`）。**不许**把它写成
# "它们不在正文里"——它们在，只是不承担主题。
_WORDY = re.compile(r"[a-zA-Z一-鿿]")
_ASCII_ALPHA = re.compile(r"[a-zA-Z]")
_PURE_CJK3 = re.compile(r"^[一-鿿]{3}$")

# S3 的第二半：中文功能词 2/3-gram。它们跨篇高频（df 门多半已拦住），但语料里总有几个
# 恰好只落在申报的那两篇里（"这个/那个/我们"在短文档里很常见）——做断言词等于恒真。
# 纪律：这份表**只放纯句法词**（虚词、代词、连接词），不放任何名词/动词——语义词一律
# 交给 S4 的 df 门判，否则又是"人抄的词表"当家。
# 依据：`rag_python_is` 的 `["值", "身份"]` 对任何回答几乎恒真（20260924 漂移哨兵报出）。
_FUNCTION_WORDS = (
    "这个", "那个", "这些", "那些", "这样", "那样", "这里", "那里", "什么", "怎么",
    "我们", "你们", "他们", "自己", "一个", "一些", "可以", "不能", "不是", "没有",
    "已经", "还是", "或者", "但是", "然后", "因为", "所以", "如果", "以及", "时候",
    "问题", "办法", "地方", "东西", "事情", "情况", "方式", "结果", "上面",
    "下面", "前面", "后面", "里面", "外面", "之后", "之前", "的话", "一样", "这么",
    "那么", "非常", "特别", "比较", "有点", "一直", "一下", "一点", "真的", "确实",
)


# 缺失语义族（漂移哨兵用）：这些词做**正**断言时判的是"如实说没有"（否定/拒绝语义），
# 不是主题词——它们的成立与语料无关，不参与 ORPHAN/GENERIC 判定，只打印计数。
_ABSENCE_WORDS = ("没有", "没找到", "未收录", "不存在", "找不到", "未找到", "暂无", "无此",
                  "没写", "帮不上", "没法", "无法", "对不上", "不能", "查不到", "看不到",
                  "没看到", "不清楚", "不掌握", "记录里", "这次执行")

# 漂移哨兵的扫描范围：**只有这些用例的期望来自文章语料**。
# 反面例子（被排除的）：贴纸（`:害羞:`）、特效（樱花/蓝）、执行记忆（执行记录）、
# 当前文章正文（欧洲）——它们的正断言判的是"系统状态/人设/别的数据源"，语料不是来源，
# 拿 ORPHAN 去判它们只会刷出一屏噪音，把真信号淹掉。范围外的**计数会打印**，不静默丢。
_SCOPE_TAGS = ("rag", "recall")


def doc_key(doc: dict) -> str:
    """文档 → `type:id`（与 `recall_eval.QUERIES` 的 expected 同一形态）。"""
    return f"{doc.get('type') or 'note'}:{doc.get('id')}"


def _doc_text(doc: dict) -> str:
    """文档的检索文本（标题 + 正文），与索引建库时用的同一形态。"""
    return str(doc.get("title") or "") + "\n" + str(doc.get("content") or "")


def _norm_keys(keys) -> list[str]:
    """`"note:14"` / `["note:14","note:22"]` / `"note:14,note:22"` 都收（CLI 好用）。"""
    if keys is None:
        return []
    if isinstance(keys, str):
        keys = keys.split(",")
    return [str(k).strip() for k in keys if str(k).strip()]


def _load_docs(docs=None) -> tuple[list[dict], dict]:
    """→ (语料文档列表, diag)。`docs` 给定就用给定的（内联夹具/离线）。

    `docs=None` 走索引快照；快照空则建一次；仍空 ⇒ `([], {"unavailable": True})`
    ——**不抛异常、也不静默**（调用方见 `unavailable` 必须响亮报"未评估"）。
    """
    if docs is not None:
        return list(docs), {}
    from rag.search import get_index
    idx = get_index()
    snap = idx.docs_snapshot()
    if not snap:
        try:
            idx.build()
        except Exception as e:  # 建索引要联网：失败是环境事实，不该炸掉整轮评测
            return [], {"unavailable": True, "why": f"build 失败：{type(e).__name__}"}
        snap = idx.docs_snapshot()
    if not snap:
        return [], {"unavailable": True, "why": "语料快照为空"}
    return snap, {}


def derive(doc_keys, *, docs=None, df_max: int = 2, strict: bool = False,
           min_doc_chars: int = 2000, cap: int | None = 40) -> tuple[list[str], dict]:
    """从申报的文档里派生"专属术语"。→ `(terms, diag)`。

    `doc_keys`：`"note:14"` 或 `["note:14", "note:22"]`（gold 里的 `doc` 两种都写）。
    `strict=True` ⇒ 只要 df==1 的词（单一来源）；默认 df ≤ `df_max`。
    `min_doc_chars`：申报文档短于这个数 ⇒ 记进 `diag["short_docs"]`（可读性事实，
      不改变候选集——截断候选会让"派生集小"更难诊断）。
    `cap`：**默认 40 是给人看的**（`--show` 回显）；`cap=None` ⇒ 不截断。
      **判据侧（`require_doc_terms`）必须用 `cap=None`**：截断按 `df` 升序取前 40，
      实测 11 篇语料下取到的全是 df=1 的最冷僻标识符，而"回答扎根于这两篇"时模型
      写出来的多半是那两篇里**常见**的词 ⇒ 截断会把诚实的回答判红（假红比放宽更糟）。
      代价是"未截断的集合很大时 `min_terms` 判据力弱"——这一点由漂移哨兵按
      `n_kept` 如实报出来，并按实跑校准 `min_terms`，不在这里假装它严。
    `diag["thin"]`：存活的术语 < 2 ⇒ 这份申报文档派生不出有判据力的术语集，
    调用方/哨兵要按 THIN 报出来，别让"派生集只有 1 个词"冒充严判据。
    """
    keys = _norm_keys(doc_keys)
    all_docs, diag = _load_docs(docs)
    if not keys:
        diag["no_doc"] = True
        return [], diag
    if not all_docs:
        diag.setdefault("unavailable", True)
        return [], diag
    by_key = {doc_key(d): d for d in all_docs}
    missing = [k for k in keys if k not in by_key]
    declared = [by_key[k] for k in keys if k in by_key]
    if missing:
        diag["missing"] = missing          # 申报了一篇语料里没有的文档 ⇒ 期望过期
    if not declared:
        return [], diag

    def _text(d: dict) -> str:
        return _doc_text(d)

    diag["short_docs"] = [doc_key(d) for d in declared if len(_text(d)) < min_doc_chars]
    # S4 的分母：整个语料的**篇级** df（同一篇里出现多次只算 1）。
    df: dict[str, int] = {}
    for d in all_docs:
        for t in set(tokenize(_text(d))):
            df[t] = df.get(t, 0) + 1
    tf: dict[str, int] = {}
    for d in declared:
        for t in tokenize(_text(d)):
            tf[t] = tf.get(t, 0) + 1

    kept: list[str] = []
    for t, _n in tf.items():
        if len(t) < 2 or not _WORDY.search(t):           # S1 / S2
            continue
        if t in _QUERY_STOPWORDS or t in _FUNCTION_WORDS:  # S3
            continue
        f = df.get(t, 0)
        if (f == 1) if strict else (f <= df_max):        # S4
            kept.append(t)
    # 排序档：0 = 有字母且无点号（`esp_https_ota`）、1 = 有字母带点号（`v1.2`/`mqtt://…`）、
    # 2 = 中文 gram。三档而不是两档：档 0/1 都"含 ASCII 字母"，但档 1 是配置路径/版本号一类
    # 非词形，混在一起会让 `cap` 只截到它（S5 只说"不排除"，没说"排前面"）。
    kept.sort(key=lambda t: (0 if (_ASCII_ALPHA.search(t) and "." not in t) else
                             1 if _ASCII_ALPHA.search(t) else 2,
                             df.get(t, 0), -len(t), -tf.get(t, 0), t))
    out = kept if cap is None else kept[:cap]
    # `top` 是"最像专有术语"的前几个（判据没命中时，人要看的是**回答本该用到什么词**）。
    # `common` 反过来按篇内词频排：一个真的在讲这两篇的回答，多半会用到这几个常用词——
    # 判据消息与漂移哨兵的"建议替换词"都用它（对着一张 `esp_mqtt_client_subscribe`
    # 想不出该写什么，对着 `固件/设备/配置` 就能）。
    diag.update(n_terms=len(out), n_kept=len(kept),
                docs=[doc_key(d) for d in declared], df_max=df_max, strict=strict,
                marks={"id_like": [t for t in out if "." in t],        # S5：只标记
                       "gram3": [t for t in out if _PURE_CJK3.match(t)]},
                top=[{"term": t, "df": df.get(t, 0), "tf": tf.get(t, 0)} for t in out[:10]],
                common=sorted(out, key=lambda t: (-df.get(t, 0), -tf.get(t, 0), t))[:8])
    if len(out) < 2:
        diag["thin"] = True
    return out, diag


def hit_terms(terms, text) -> list[str]:
    """正文里命中了哪些申报术语（大小写不敏感子串）。返回命中列表，供判据数个数。"""
    low = str(text or "").lower()
    return [t for t in (terms or []) if str(t).lower() in low]


def _literal_df(docs: list[dict], word: str) -> int:
    """字面（子串）落在多少篇里——**与 token 的 df 不是一回事**。

    `Content-Type` 是最好的反例：它字面就在 note:12 的正文里，词频 df 却是 0，因为
    `tokenize` 按字符类把 `-` 切掉、只剩 `content`/`type` 两个 token。哨兵若只报 token df，
    会把一条**有语料依据**的断言误判成死支（这正是 20260925 差点发生的事）。
    """
    return sum(1 for d in docs if word.lower() in _doc_text(d).lower())


def _token_df(docs: list[dict], word: str) -> int:
    """这个词（当 token 时）在多少篇里出现；切碎的词取它切出来的**最长** token 的 df。"""
    toks = tokenize(word)
    if not toks:
        return 0
    df: dict[str, int] = {}
    for d in docs:
        for t in set(tokenize(_doc_text(d))):
            df[t] = df.get(t, 0) + 1
    return max((df.get(t, 0) for t in toks), default=0)


def drift(*, case: str = "", docs=None, df_max: int = 2, generic_df: int = 4,
          report_dir: str = "eval/report") -> int:
    """**漂移哨兵**：扫 golden 的词表型断言，报出四类"看着在、其实不在"的判据。

    人抄的词表会随语料漂移（`rag_ota_http` 就是现场：期望词的前提"某篇已下架"过期了），
    而漂移的表现是**用例继续绿或持续红，没人知道判据已经不成立**。四类：

      ORPHAN    词在**全库**都不出现 ⇒ 模型只要"猜"就能过（假阳性通路）；
      GENERIC   词字面落在 ≥`generic_df` 篇里 ⇒ 恒真（对任何回答都可能成立）；
      MISBOUND  该用例申报了 `doc`，而这个词**不在申报篇里** ⇒ 词表与申报互相矛盾；
      THIN      申报篇派生不出 ≥2 个术语 ⇒ 判据力不足（别用"派生集只有 1 个词"冒充严判据）。

    **只判"能判的"**：缺失语义族（没有/找不到/未收录…）是**否定**语义，不是主题词，
    按小词表白名单跳过并打印计数（语料里 `没有` 字面就在 4 篇里，拿"满语料"去判它毫无意义）。
    没有申报 `doc` 的用例仍扫 ORPHAN/GENERIC，但**给不出建议替换词**（没有建议来源）。
    **扫描范围**见 `_SCOPE_TAGS`：只扫"期望来自文章语料"的用例，其余**计数但不判**（范围外的
    正断言判的是贴纸名单/特效开关/执行记忆/当前文章正文等别的数据源，拿 ORPHAN 判它们
    只会刷一屏噪音——首次上线实测 38 条全是此类）。范围外计数照打，不静默丢。

    报告落 `eval/report/corpus_drift_<ts>.md`（与 `review_*.md`/`reconcile_*.md` 同族）；
    有 ORPHAN/MISBOUND ⇒ 非零退出。**非门禁**：它是"给人看的哨兵"，不是 CI 闸。
    """
    import json as _json
    from datetime import datetime

    all_docs, diag = _load_docs(docs)
    if diag.get("unavailable") or not all_docs:
        print(f"[drift] 语料不可用（{diag.get('why') or '快照为空'}）—— 未评估，不写报告")
        return 1
    cases_file = Path(__file__).resolve().parent / "golden" / "basic.jsonl"
    cases = [_json.loads(ln) for ln in
             cases_file.read_text(encoding="utf-8").splitlines() if ln.strip()]
    if case:
        cases = [c for c in cases if c.get("id") == case]
        if not cases:
            print(f"[drift] 没有这条用例：{case}", file=sys.stderr)
            return 2

    corpus_keys = [doc_key(d) for d in all_docs]
    orphan, generic, misbound, thin, skipped, undeclared = [], [], [], [], [], []
    declared_rows = []  # 申报概览：(用例, 申报篇, 派生集大小)——`--case X` 时它是主要输出
    n_words, out_of_scope = 0, []
    for c in cases:
        cid, gold = c.get("id"), (c.get("gold") or {})
        specs = gold.get("require_doc_terms") or []
        if not (set(c.get("tags") or []) & set(_SCOPE_TAGS) or specs):
            out_of_scope += [(cid, w) for w in (gold.get("text_contains") or [])]
            continue
        declared = [k for s in specs for k in _norm_keys(s.get("doc"))]
        # 申报篇的派生集：既是 THIN 的判据，也是建议替换词的来源。
        suggestion: dict[str, list] = {}
        for s in specs:
            keys = _norm_keys(s.get("doc"))
            terms, tdiag = derive(keys, docs=all_docs, df_max=int(s.get("df_max", df_max)),
                                  strict=bool(s.get("strict", False)), cap=None)
            if tdiag.get("thin"):
                thin.append((cid, keys, tdiag.get("n_kept")))
            declared_rows.append((cid, keys, tdiag.get("n_kept")))
            for k in keys:
                suggestion[k] = tdiag.get("common") or tdiag.get("top") or []
        for w in (gold.get("text_contains") or []):
            n_words += 1
            if any(a in str(w) for a in _ABSENCE_WORDS):
                skipped.append((cid, w))
                continue
            lit, tdf = _literal_df(all_docs, str(w)), _token_df(all_docs, str(w))
            row = (cid, w, lit, tdf, len(all_docs))
            if lit == 0:
                orphan.append(row)
            elif lit >= generic_df:
                generic.append(row)
            if declared and not any(
                    str(w).lower() in _doc_text(d).lower()
                    for d in all_docs if doc_key(d) in declared):
                misbound.append(row)
            if not declared:
                undeclared.append((cid, w))

    rc = 1 if (orphan or misbound) else 0
    md = _drift_md(corpus_keys, cases_file, cases, orphan, generic, misbound, thin,
                   skipped, undeclared, n_words, df_max, generic_df, out_of_scope,
                   declared_rows)
    Path(report_dir).mkdir(parents=True, exist_ok=True)
    out = Path(report_dir) / f"corpus_drift_{datetime.now():%Y%m%d_%H%M%S}.md"
    out.write_text(md, encoding="utf-8")
    print(f"[drift] 范围内词 {n_words} 个：ORPHAN {len(orphan)} / GENERIC {len(generic)} / "
          f"MISBOUND {len(misbound)} / THIN {len(thin)} / 跳过(缺失语义族) {len(skipped)} "
          f"/ 无申报 {len(undeclared)}；范围外（期望不来自语料）{len(out_of_scope)} 个")
    for cid, keys, n in declared_rows:
        print(f"  申报     {cid}: {'、'.join(keys)} ⇒ 派生集 {n} 个词")
    for r in orphan:
        print(f"  ORPHAN   {r[0]}: {r[1]!r}（字面命中 0/{r[4]} 篇）")
    for r in misbound:
        print(f"  MISBOUND {r[0]}: {r[1]!r}（不在申报篇里；字面 {r[2]}/{r[4]} 篇）")
    print(f"[drift] 报告 → {out}" + ("" if rc == 0 else "（有 ORPHAN/MISBOUND ⇒ 非零退出）"))
    return rc


def _drift_md(corpus_keys, cases_file, cases, orphan, generic, misbound, thin,
              skipped, undeclared, n_words, df_max, generic_df, out_of_scope,
              declared_rows) -> str:
    from datetime import datetime
    L = [f"# 语料漂移哨兵（{datetime.now():%Y-%m-%d %H:%M:%S}）", "",
         f"- 语料：{len(corpus_keys)} 篇 — {'、'.join(corpus_keys)}",
         f"- 用例：{cases_file.name} 取 {len(cases)} 条；**范围内**（标签 ∈ {_SCOPE_TAGS} "
         f"或带 `require_doc_terms`）的 `text_contains` 词 {n_words} 个，"
         f"范围外 {len(out_of_scope)} 个",
         f"- 口径：字面命中 0 ⇒ ORPHAN；字面落在 ≥{generic_df} 篇 ⇒ GENERIC；"
         f"申报篇派生 df≤{df_max}；**只判能判的**（缺失语义族跳过 {len(skipped)} 个）", ""]

    def table(title, rows, note):
        L.append(f"## {title}（{len(rows)}）")
        L.append("")
        L.append(note)
        if not rows:
            L.append("")
            L.append("无。")
            L.append("")
            return
        L.append("")
        L.append("| 用例 | 词 | 字面命中 | token df | 语料篇数 |")
        L.append("|---|---|---|---|---|")
        for cid, w, lit, tdf, n in rows:
            L.append(f"| {cid} | `{w}` | {lit} | {tdf} | {n} |")
        L.append("")

    table("ORPHAN：词在全库都不出现 ⇒ 模型猜对即可通过",
          orphan,
          "这些词在语料里找不到任何依据：一条**编造**的回复只要凑出这个词就能通过断言。"
          "要么删词，要么换成有语料依据的词（见各用例的申报篇派生集）。")
    table("MISBOUND：词不在该用例申报的篇里",
          misbound,
          "用例一边说\"回答必须扎根这几篇\"、一边把正断言押在**别的篇**的词上——两处期望互相"
          "矛盾，模型无论扎根哪边都可能红。（`rag_ota_http` 20260925 之前就是这个形状的变体。）")
    table(f"GENERIC：字面落在 ≥{generic_df} 篇里 ⇒ 恒真",
          generic,
          "太常见 ⇒ 判不出\"扎没扎根\"。注意与 `Content-Type` 那类**分词**假象区分："
          "本表的\"字面命中\"是子串计数，与 token df 分列两栏。")
    L.append(f"## THIN：申报篇派生不出 ≥2 个术语 ⇒ 判据力不足（{len(thin)}）")
    L.append("")
    L.append("申报的那一篇太短/太同质，派生集撑不起 `min_terms`：要么放宽 `df_max`，要么挑更对口的一篇。")
    L.append("")
    if not thin:
        L.append("无。")
    else:
        L.append("| 用例 | 申报篇 | 派生集大小 |")
        L.append("|---|---|---|")
        for cid, keys, n in thin:
            L.append(f"| {cid} | {'、'.join(keys)} | {n} |")
    L.append("")

    L.append(f"## 申报概览：`require_doc_terms` 声明了什么、派生集多大（{len(declared_rows)} 条申报）")
    L.append("")
    L.append("派生集大小 = 该申报篇在 `df≤%d` 下能取出的术语个数（判据侧 `cap=None`）。"
             "本表也是 `--case X` 的主要输出：那条用例若已把词表换成申报，`text_contains` 词数会是 0，"
             "\"判据还成不成立\"只能从这里读。" % df_max)
    if declared_rows:
        L.append("")
        L.append("| 用例 | 申报篇 | 派生集大小 |")
        L.append("|---|---|---|")
        for cid, keys, n in declared_rows:
            L.append(f"| {cid} | {'、'.join(keys)} | {n} |")
    else:
        L.append("")
        L.append("无。")
    L.append("")

    L.append(f"## 跳过：缺失语义族（{len(skipped)}）")
    L.append("")
    L.append("「没有/找不到/未收录…」是**否定**语义，不是主题词——它们做正断言时判的是"
             "\"如实说没有\"，不受语料漂移影响（所以不参与 ORPHAN/GENERIC 判定）。")
    if skipped:
        L.append("")
        L.append("· " + "；".join(f"{cid}:`{w}`" for cid, w in skipped))
    L.append("")

    L.append(f"## 无申报文档的用例（{len(set(c for c, _ in undeclared))} 条用例）")
    L.append("")
    L.append("这些用例没有 `require_doc_terms`，所以**给不出建议替换词**（没有申报篇就没有派生集）。"
             "它们的词只按 ORPHAN/GENERIC 判。要拿到建议，先补申报："
             "`.venv/bin/python eval/corpus_terms.py --show note:X` 读一遍再写。")
    if undeclared:
        L.append("")
        L.append("· " + "；".join(f"{cid}:`{w}`" for cid, w in undeclared[:40]))
        if len(undeclared) > 40:
            L.append(f"· …其余 {len(undeclared) - 40} 个")
    L.append("")
    L.append(f"## 范围外：期望不来自文章语料的用例（{len(out_of_scope)} 个词）")
    L.append("")
    L.append(f"扫描范围 = 标签含 {'/'.join(_SCOPE_TAGS)} 或带 `require_doc_terms` 的用例。"
             "范围外那些用例的正断言判的是**别的数据源**，不是这份 RAG 快照：贴纸名"
             "（8 个 `:名字:` 是 prompts 里的名单）、页面特效/夜间开关、`recent_executions` "
             "执行记忆、当前文章正文、工具返回本身（`cq_*` 类走 `search_notes` 读库、"
             "时间/设备类走 IoT 接口——库里有没有这个词与 RAG 索引快照是两件事）。"
             "拿 ORPHAN/GENERIC 去判它们只会刷一屏噪音（首次上线实测 38 条全是此类），"
             "所以**排除但计数**——不静默丢，范围本身将来要改时有据可查。")
    if out_of_scope:
        L.append("")
        L.append("· " + "；".join(f"{cid}:`{w}`" for cid, w in out_of_scope[:40]))
        if len(out_of_scope) > 40:
            L.append(f"· …其余 {len(out_of_scope) - 40} 个")
    L.append("")
    L.append("## 建议替换词从哪来")
    L.append("")
    L.append("`derive()` 的 `common`（申报篇里最常用的几个词）——**不是** `top`（那是最冷僻的"
             "专有术语形态，人对着它想不出该写什么）。同一份口径见 `--show` 的 `[常用]` 行。")
    return "\n".join(L) + "\n"


def _show(keys: list[str], *, df_max: int, strict: bool, docs=None) -> int:
    """逐篇 + 合并地打印派生结果（迁移前"读该问题最贴的那一篇"的落地工具）。"""
    print(f"[corpus_terms] 语料来源：{'内联夹具' if docs is not None else '线上快照'}"
          f" | df_max={df_max} strict={strict}")
    if len(keys) > 1:
        terms, diag = derive(keys, docs=docs, df_max=df_max, strict=strict)
        _dump("申报集 " + ",".join(keys), terms, diag)
    rc = 0
    for k in keys:
        terms, diag = derive([k], docs=docs, df_max=df_max, strict=strict)
        _dump(k, terms, diag)
        if diag.get("unavailable") or diag.get("missing"):
            rc = 1
    return rc


def _dump(title: str, terms: list[str], diag: dict) -> None:
    if diag.get("unavailable"):
        print(f"\n{title}: **语料不可用** —— 未评估（{diag.get('why') or '快照为空'}）")
        return
    if diag.get("missing"):
        print(f"\n{title}: 申报的文档不在语料里：{diag['missing']}（期望过期？）")
        return
    print(f"\n{title}: 派生 {diag.get('n_kept', 0)} 个术语"
          f"（df≤{diag.get('df_max')}{'，strict' if diag.get('strict') else ''}"
          f"，取前 {len(terms)}）"
          + ("  ⚠️ THIN（<2 个：判据力不足）" if diag.get("thin") else "")
          + (f"  ⚠️ 短文档 {diag['short_docs']}" if diag.get("short_docs") else ""))
    for row in diag.get("top") or []:
        print(f"    {row['term']:<28} df={row['df']} tf={row['tf']}")
    if diag.get("common"):
        print(f"    [常用] {diag['common']}")
    marks = diag.get("marks") or {}
    if marks.get("id_like") or marks.get("gram3"):
        print(f"    [S5 标记] id_like={marks.get('id_like')} gram3={marks.get('gram3')}")


def main(argv: list[str]) -> int:
    args = list(argv[1:])
    keys: list[str] = []
    df_max, strict, do_drift, case = 2, False, False, ""
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--show":
            keys = _norm_keys(args[i + 1]) if i + 1 < len(args) else []
            i += 2
        elif a.startswith("--show="):
            keys = _norm_keys(a.split("=", 1)[1])
            i += 1
        elif a == "--df-max":
            df_max = int(args[i + 1])
            i += 2
        elif a == "--strict":
            strict = True
            i += 1
        elif a == "--drift":                      # 漂移哨兵（扫 golden 的词表型断言）
            do_drift = True
            i += 1
        elif a == "--case":                       # 只扫一条用例
            case = args[i + 1] if i + 1 < len(args) else ""
            i += 2
        else:
            print(f"未知参数：{a}", file=sys.stderr)
            print(__doc__.strip().split("用法：")[-1].strip(), file=sys.stderr)
            return 2
    if do_drift:
        return drift(case=case, df_max=df_max)
    if not keys:
        print("需要 `--show note:14[,note:22]`（或 `--drift [--case X]`）", file=sys.stderr)
        return 2
    return _show(keys, df_max=df_max, strict=strict)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
