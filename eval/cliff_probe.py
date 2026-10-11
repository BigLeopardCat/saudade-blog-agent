#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""断崖证书探针：**真语料上**跑有/无断崖两臂，断言 top-3 一个字都没动。

要治的病（20261010 现场）：相对断崖（α=0.25）此前只有 20260920 那次实跑签发的**经验
证书**——代码注释里那句「α≤0.25 时 recall@1/@3 与截断前完全一致」。证书是**一次快照**：
它拿当时的语料签发，此后没有任何东西复验。语料 12→13 篇（新增 note:62，一篇与多条
query 同主题的强势文档）当天就把它作废——top1 抬高 ⇒ `floor = top1×0.25` 跟着抬高 ⇒
**排在第 2/3 名的真答案被自己的断层切掉**：`rag_eval_system` 的 gold 在断崖前排第 2、
断崖后**整条从候选里消失**（hits 由 `['note:62','note:19','note:46']` 收成 `['note:62']`），
recall@3/@5 由 1.0000 掉到 0.9231、MRR 由 0.8718 掉到 0.8333，而**没有任何判据会指认
凶手是断崖**——是作者手工比对前后两轮报告才定位的（同日 recall@1 由 0.9231 掉到 0.7692
是**语料长大的排序变化**，两形状都一样，与本探针无关，别混）。
一条只在"语料恰好没长出强势文档"时成立的判据，等于没有判据。

现在保底 top-K（`rag/search.py::_CLIFF_MIN_KEEP`）把那条经验证书变成了**结构不变量**：
断崖只裁第 K 名之后的尾巴 ⇒「断崖改变 recall@1/@3」在结构上不可能。本探针就是这条
不变量的**真语料**验证（合成语料那一半在 `tests/test_cliff_min_keep.py`，进 CI）：

    同一次评测里对每条 query 跑两臂（线上那臂 + `apply_cliff=False` 那臂），
    逐条比名次 —— 只要有一条 gold 在 top-3 里而两臂名次不同，就是保底破了。

**为什么必须用真语料**：合成夹具证明不了"今天的语料里有没有那种强势文档"。病灶是
**语料形态**的函数，而语料每发一篇文章就变一次 ⇒ 它是一次**每天都在变的输入**上的断言，
只能每天跑（离线套件的语料是内联的，对语料漂移完全瞎）。

**退出码**（与 `golden_fixture.py` 同族：0 干净 / 非 0 说清是哪一种）：
  0 = 两臂在 top-3 内逐条一致（证书成立）
  1 = **有回归**：断崖动了 top-3 里的名次（先看那行 ❌ 点名的 query，别读通过率）
  2 = **无法确认**：语料读不到（Rust API 没起来 / 翻页失败）——"读不到"不是"没有回归"

**非门禁**（同语料漂移哨兵那一族）：它报的是"那条结构约束破了"，而真破了的时候
`eval/recall_eval.py` 的报告里也已经写着同一件事。挂成门禁会让整夜被一条**观察**带红；
不挂的代价是"要有人看报告"——**所以它把结论写进夜间日志的那一行里**。

用法：
  .venv/bin/python eval/cliff_probe.py            # 主集 + 留出集
  .venv/bin/python eval/cliff_probe.py --main     # 只跑主集（快一半）
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT.parent))
sys.path.insert(0, str(ROOT))

import recall_eval as re  # noqa: E402  同目录：**复用同一套度量代码**，不另写一份两臂比较


def _report(name: str, rep: dict) -> list[str]:
    """打印一格，返回点名的那几条（空 = 这一格绿）。"""
    cf = rep["cliff"]
    print(f"  断崖 α={cf['ratio']} 保底 top-{cf['min_keep']}："
          f"裁掉 {cf['candidates_cut']} 条候选（{cf['queries_cut']}/{rep['n']} 条 query 动过候选）")
    print(f"  断崖前 recall@1={cf['recall@1_precliff']:.4f} @3={cf['recall@3_precliff']:.4f} "
          f"@5={cf['recall@5_precliff']:.4f}")
    print(f"  线上   recall@1={rep['recall@1']:.4f} @3={rep['recall@3']:.4f} @5={rep['recall@5']:.4f}")
    if cf["rank_changed"]:
        # 只报"top-3 内名次变了"那几条——@5 上 gold 原本排 4/5 名被裁是**断崖该有的代价**，
        # 混进来会让这一行永远在响（哨兵一响就没人看了）。
        print(f"  ❌ {name}：保底破了——断崖动了 top-3 里的名次："
              + "、".join(f"{c['id']} rank {c['rank_precliff']}→{c['rank']}"
                          for c in cf["rank_changed"]))
        return [c["id"] for c in cf["rank_changed"]]
    print(f"  ✅ {name}：两臂在 top-3 内逐条一致（那条结构约束今天成立）")
    return []


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--main", action="store_true", help="只跑主集（默认主集 + 留出集）")
    args = ap.parse_args(argv)

    idx = re.get_index()
    try:
        idx.build()
    except Exception as e:
        print(f"❌ 语料读不到（{type(e).__name__}: {e}）——**这一夜没验**，不是「没有回归」")
        return 2
    docs = idx._docs
    if not docs:
        # build() 自己吞异常（重建失败沿用旧索引，见 `_lexical_ranked`），所以这里再判一次
        print("❌ 语料为空（公开 API 没起来 / 翻页失败）——**这一夜没验**，不是「没有回归」")
        return 2
    print(f"语料：{len(docs)} 篇（断崖证书是**语料形态**的函数 ⇒ 每发一篇文章它都该重跑一次）")

    bad: list[str] = []
    print("\n== 主集 ==")
    bad += _report("主集", re.evaluate(idx, show=False))
    if not args.main:
        print(f"\n== 留出集（{len(re.HOLDOUT)} 条）==")
        bad += _report("留出集", re.evaluate(idx, show=False, queries=re.HOLDOUT))

    print()
    if bad:
        print(f"❌ {len(bad)} 条 query 的 top-3 名次被断崖改动：" + "、".join(bad))
        print("   ⇒ 先看 `rag/search.py::_lexical_ranked` 那三行是不是被写回了整列过滤，"
              "别先去调 α（α 只能让伤口大小变，保底才是止血的那条）。")
        return 1
    print("✅ 断崖未动任何一条 top-3 名次（证书成立）")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
