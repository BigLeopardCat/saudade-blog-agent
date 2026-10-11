# -*- coding: utf-8 -*-
"""相对断崖的**保底 top-K**（`rag/search.py::_CLIFF_MIN_KEEP`）的离线自测。

**为什么单起一套**：断崖（α=0.25）此前只有一张**经验证书**——20260920 那次实跑的注释
「α≤0.25 时 recall@1/@3 与截断前完全一致」。证书是**一次快照**：它拿当时的语料签发，
此后没有任何东西复验。20261010 语料 12→13 篇（新增 note:62，一篇与若干 query 同主题的
强势文档）当场把它作废——top1 抬高 ⇒ `floor = top1×0.25` 跟着抬高 ⇒ **排在第 2/3 名的
真答案被自己的断层切掉**。`rag_eval_system` 的 gold 在断崖前排第 2、断崖后**整条从候选
里消失**（hits 由 `['note:62','note:19','note:46']` 收成 `['note:62']`）⇒ recall@3/@5
1.0000→0.9231、MRR 0.8718→0.8333，而**没有任何判据会指认凶手是断崖**（作者手工比对两轮
报告才定位的）。

所以本套件锁的不是"α 取多少合适"，而是**保底那条结构约束**：断崖只允许裁第 K 名之后
的尾巴，前 K 名任何情况都不动 ⇒「断崖改变 recall@1/@3」在结构上不可能发生。

| 段 | 判什么 | 不锁会怎样 |
|---|---|---|
| ① | **夹具成立**：旧形状（整列过滤）在这批语料上确实切掉真答案 | 夹具没做出病灶 ⇒ 整套恒绿（正控不红不算数） |
| ② | 保底内的候选与名次在开/关断崖两臂**逐条一致** | 强势文档一进语料，真答案被静默切掉 |
| ③ | 尾巴**确实被裁**（关臂长于开臂） | 断崖退化成 no-op，"保底"变成"截断没了" |
| ④ | 开关契约：`apply_cliff` 默认 True == 线上形态 | 两臂跑的是同一臂（hybrid 那次的同款坑） |
| ⑤ | 判据同源：`cliff_config()` 与实现一致 | 报告里的 α / K 是手抄的，抄一次漂一次 |

夹具是 20261010 现场的**最小复现**：一篇强势文档（query 里五个词都反复命中）+ 两篇
真答案（各只命中一个词）——分数断层正好卡在真答案头顶。秒级、无网络：语料内联，
`_fetch_corpus` 被短路、向量预热被摘掉。

**红基线（先武装判据再读）**——把 `rag/search.py` 那行改回整列过滤
（`ranked = [r for r in ranked if r["score"] >= floor]`）⇒ ② 红；把 `_CLIFF_RATIO`
改成 0.0 ⇒ ③ 红（尾巴一点都不裁）；把 `cliff_config()` 的 `min_keep` 写死成 9 ⇒ ⑤ 红。

用法：.venv/bin/python tests/test_cliff_min_keep.py
"""
from __future__ import annotations

import inspect
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import rag.search as rs  # noqa: E402

FAILED: list[str] = []


def check(name: str, ok: bool, extra: str = "") -> None:
    print(f"  {'✅' if ok else '❌'} {name}" + (f"  [{extra}]" if extra else ""))
    if not ok:
        FAILED.append(name)


# ── 夹具：一篇"强势文档" + 两篇真答案 + 三篇尾巴 ─────────────────────────
# 强势文档把 query 的五个实词各命中 4 次 ⇒ 分数（≈7.6）远高于后面每篇（≈0.4–1.1），
# `top1×0.25 ≈ 1.9` 那条线因此**卡在真答案头顶**——这正是 20261010 的形状。
_QUERY = "架构 端口 后端 前端 技术栈 部署 链路"

_FIXTURE = [
    {"type": "note", "id": 90, "title": "架构文档",
     "content": "## 架构总览\n" + "架构 端口 后端 前端 技术栈 部署 链路 " * 4},
    # 真答案之一：命中「端口」两次 ⇒ 分 ≈1.07（旧形状下这条会被切掉）
    {"type": "note", "id": 91, "title": "OTA 问题与解决记录",
     "content": "## 端口\n平台侧 OTA 服务监听 3100 的端口。"},
    # 真答案之二：同样命中「端口」
    {"type": "note", "id": 92, "title": "部署运维笔记",
     "content": "## 服务\n后端跑在 127.0.0.1:3000 的端口上。"},
    # 尾巴三篇：各命中间一个词一次 ⇒ 分数最低，断崖该裁的就是它们
    {"type": "note", "id": 93, "title": "杂记 A", "content": "## 记\n顺手记一下部署这件事。"},
    {"type": "note", "id": 94, "title": "杂记 B", "content": "## 记\n链路有时候会断。"},
    {"type": "note", "id": 95, "title": "杂记 C", "content": "## 记\n后端偶尔会重启。"},
]


def _build_fixture_index() -> rs.RagIndex:
    """离线索引：短路语料拉取 + 摘掉向量预热（后者会联网）。"""
    rs.warm_vectors = lambda chunks: None  # type: ignore[assignment]
    ix = rs.RagIndex()
    ix._fetch_corpus = lambda: [dict(d) for d in _FIXTURE]  # type: ignore[method-assign]
    ix.build()
    return ix


ix = _build_fixture_index()
rs._index = ix  # get_index() 的进程级单例指向夹具

CFG = rs.cliff_config()
K = CFG["min_keep"]


def _keys(rows: list[dict]) -> list[str]:
    return [f"{r['type']}:{r['id']}" for r in rows]


def _scores() -> tuple[list, list]:
    """(开臂候选, 关臂候选)——两臂都用 top_k=5，只差断崖一下。"""
    return (rs.search(_QUERY, top_k=5, apply_cliff=True),
            rs.search(_QUERY, top_k=5, apply_cliff=False))


_ON_ROWS, _OFF_ROWS = _scores()
_ON, _OFF = _keys(_ON_ROWS), _keys(_OFF_ROWS)
# 旧形状（整列按 top1×α 过滤）在这批语料上的结果——**夹具成立性**的证据，见 ①
_OLD = _keys([r for r in _OFF_ROWS
              if r["score"] >= _OFF_ROWS[0]["score"] * CFG["ratio"]])

print(f"① 夹具成立：旧形状（整列过滤）必须真的切掉真答案，否则下面几段是空断言")
print(f"     开臂 {[(k, r['score']) for k, r in zip(_ON, _ON_ROWS)]}")
print(f"     关臂 {[(k, r['score']) for k, r in zip(_OFF, _OFF_ROWS)]}")
print(f"     旧形状 would-be {_OLD}")
check("强势文档坐稳 top-1（断层真的存在）", _OFF[:1] == ["note:90"], f"top1={_OFF[:1]}")
check("旧形状把候选收成 1 条（『富者愈富』的现场）", len(_OLD) == 1, f"{len(_OLD)} 条")
check("旧形状切掉了排在第 2 名、**在保底区内**的真答案 note:92",
      "note:92" not in _OLD and _OFF.index("note:92") + 1 == 2,
      f"关臂 rank={_OFF.index('note:92') + 1}")

print(f"\n② 保底 top-{K}：前 {K} 名的候选与名次必须与关臂逐条一致")
check(f"前 {K} 名逐条相同", _ON[:K] == _OFF[:K], f"{_ON[:K]} vs {_OFF[:K]}")
check("第 2 名的真答案 note:92 在两臂里都在（旧形状会把它切掉）",
      "note:92" in _ON and "note:92" in _OFF)
check("裁掉的候选全都落在保底之外",
      all(k in _OFF[K:] for k in _OFF if k not in _ON),
      f"被裁 {[k for k in _OFF if k not in _ON]}")

print("\n③ 尾巴确实被裁（断崖的产出），且不是把候选清空到只剩冠军")
check("开臂严格短于关臂", len(_ON) < len(_OFF), f"{len(_ON)} < {len(_OFF)}")
check("开臂不止一条", len(_ON) > 1, f"{len(_ON)} 条")
check("开臂以 top-1 开头（断崖只删不排）", _ON[:1] == _OFF[:1])

print("\n④ 开关契约：默认 == 线上形态")
check("不传 apply_cliff 时与显式 True 一致", _ON == _keys(rs.search(_QUERY, top_k=5)))
check("三处入口的 apply_cliff 默认都是 True",
      all(inspect.signature(fn).parameters["apply_cliff"].default is True
          for fn in (rs.RagIndex._lexical_ranked, rs.RagIndex.search, rs.search)))

print("\n⑤ 判据同源：cliff_config() 报的就是实现里那两个数")
check("ratio / min_keep 两个键都在且是真数", set(CFG) == {"ratio", "min_keep"}
      and isinstance(CFG["ratio"], float) and isinstance(CFG["min_keep"], int))
check("min_keep ≥ 3（保底不许被调小到看不见）", CFG["min_keep"] >= 3, str(CFG["min_keep"]))
check("cliff_config 与模块常量同源", CFG["ratio"] == rs._CLIFF_RATIO
      and CFG["min_keep"] == rs._CLIFF_MIN_KEEP)

print()
if FAILED:
    print(f"❌ {len(FAILED)} 项未通过：" + "、".join(FAILED))
    sys.exit(1)
print("✅ 全部通过")
