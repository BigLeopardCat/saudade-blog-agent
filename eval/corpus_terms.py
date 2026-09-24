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


def doc_key(doc: dict) -> str:
    """文档 → `type:id`（与 `recall_eval.QUERIES` 的 expected 同一形态）。"""
    return f"{doc.get('type') or 'note'}:{doc.get('id')}"


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
        return str(d.get("title") or "") + "\n" + str(d.get("content") or "")

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
    df_max, strict = 2, False
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
        else:
            print(f"未知参数：{a}", file=sys.stderr)
            print(__doc__.strip().split("用法：")[-1].strip(), file=sys.stderr)
            return 2
    if not keys:
        print("需要 `--show note:14[,note:22]`（或 `--drift`）", file=sys.stderr)
        return 2
    return _show(keys, df_max=df_max, strict=strict)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
