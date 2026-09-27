#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""token 用量与**前缀缓存命中率**的按天聚合（20260927）。

**为什么存在**：本 agent 的输入侧占成本 ~99%，能省的只有"落在缓存里的那部分"——
前缀缓存按渲染后字符串的**连续前缀**命中，所以"模板里哪一段算稳定前缀"是个可测量
的量。生产 trace 从 20260927 起在 `llm_done` 事件里带 `input/output/cache_read`
（见 `agent/llm_usage.py`），这个脚本就是把它读成人话的那一端：

  · 按 **节点 × 引擎** 切：planner / model / execute（屏幕文案）/ reflector；
  · **命中率** `cache_read/input` 是按"报了缓存字段的那些调用"算的，分**分母**单列
    ——`cache_read` 缺席 ≠ 0（端点没回这个字段时量不到，不是没命中，见
    `agent/llm_usage.py` 的字段契约）。把两件事混起来算，会得出"缓存完全没命中"
    这种**没数据支撑的结论**。
  · 按天一行：同一档模板改动的效果，看的是"改前那几天 vs 改后那几天"的命中率差。

**钱数只能由调用方给**：网关不回任何计价字段（已核），单价是商务数字、随时会变
⇒ 脚本里**不写价格常量**。要金额就显式传 `--price-in/--price-out/--price-cache`
（元/百万 token），不传就只打 token 数。

用法（cd saudade-blog-agent）：
  .venv/bin/python eval/token_cost_report.py                    # 生产 trace，近 7 天
  .venv/bin/python eval/token_cost_report.py --from 20260927    # 自该日起
  .venv/bin/python eval/token_cost_report.py --days 1           # 只看最近 24 小时
  .venv/bin/python eval/token_cost_report.py --dir <golden run 目录>
        # golden 一次跑完的 trace 目录：**同一次 run 里所有用例共用同一份技能菜单/
        #  规则正文** ⇒ 它就是"模板重排到底有没有把前缀抬起来"的离线判据
        #  （不用等生产流量；调用足够密集时缓存会真的命中）。
  .venv/bin/python eval/token_cost_report.py --price-in 12 --price-out 36 --price-cache 1.2

**不是门禁**：恒退出 0（同 `eval/llm_judge.py`）——它出的是报表，判据在别处
（`tests/test_llm_usage.py` 钉住记账接线，`tests/test_prompt_prefix.py` 钉住前缀长度）。
"""
import argparse
import os
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
# 枚举与读取各自只有一个实现（20260925/20260926）：布局或压缩形态一变，
# 四处读取端不会各看一半语料。
from trace_files import iter_trace_files, parse_trace_name  # noqa: E402
from trace_io import load_trace  # noqa: E402

# 生产 trace 根 = 应用配置那一处（`settings.trace_dir`），不手抄第二份路径。
from config import settings  # noqa: E402

PROD_TRACE_DIR = settings.trace_dir


def _stamp_of(path: str) -> str:
    """trace 的日期（YYYYMMDD）；命名不规范的按 mtime 落天，实在取不到记 '未知'。"""
    stamp, _uid = parse_trace_name(path)
    if not stamp:
        try:
            import time
            return time.strftime("%Y%m%d", time.localtime(os.path.getmtime(path)))
        except OSError:
            return "未知"
    return stamp[:8]


def collect(paths: list[str]) -> dict:
    """扫一遍 trace，把每条 `llm_done` 的用量按 (天, 节点, 引擎) 归集。

    判据是**事件里有没有 `input` 键**——不按事件名认死（`llm_done` 在四个节点各发一次，
    而"哪条事件带用量"是记账侧的契约，会演进）。没有 `input` 键的 llm_done 记为
    "未量到"，单列一栏：那是"这条调用没记账"，不是"用量为 0"。
    """
    agg = defaultdict(lambda: {"calls": 0, "unnamed": 0, "in": 0, "out": 0,
                               "cache_seen": 0, "cache": 0})
    traces = 0
    for p in paths:
        doc = load_trace(p)
        if not doc:
            continue
        traces += 1
        day = _stamp_of(p)
        for ev in doc.get("events") or []:
            if not isinstance(ev, dict) or "input" not in ev:
                continue
            node = ev.get("node") or "?"
            engine = ev.get("engine") or "-"
            row = agg[(day, node, engine)]
            row["calls"] += 1
            row["in"] += int(ev.get("input") or 0)
            row["out"] += int(ev.get("output") or 0)
            if "cache_read" not in ev:
                # 端点没回缓存字段：这一栏是"量不到"，不许并进命中率的分母
                row["unnamed"] = row.get("unnamed", 0) + 1
                continue
            row["cache_seen"] += 1
            row["cache"] += int(ev.get("cache_read") or 0)
    return {"traces": traces, "rows": agg}


def _sum(agg: dict, keys: list) -> dict:
    out = {"calls": 0, "unnamed": 0, "in": 0, "out": 0, "cache_seen": 0, "cache": 0}
    for k in keys:
        for f in out:
            out[f] += agg[k][f]
    return out


def _row(label: str, r: dict, price: tuple | None) -> str:
    hit = (f"{r['cache'] / (r['in'] or 1) * 100:.1f}%"
           if r["cache_seen"] else "n/a")
    tail = ""
    if price and r["cache_seen"]:
        p_in, p_out, p_cache = price
        miss = r["in"] - r["cache"]
        yuan = (miss * p_in + r["cache"] * p_cache + r["out"] * p_out) / 1e6
        no_cache = (r["in"] * p_in + r["out"] * p_out) / 1e6
        tail = f" | ¥{yuan:.2f}（不命中则 ¥{no_cache:.2f}）"
    return (f"{label:<28} {r['calls']:>5} {r['in']:>10} {r['out']:>7} "
            f"{r['cache']:>10} {hit:>7} {r['calls'] - r['cache_seen']:>5}{tail}")


_HEAD = (f"{'分组':<28} {'调用':>5} {'输入tok':>10} {'输出tok':>7} "
         f"{'命中tok':>10} {'命中率':>7} {'未量到':>5}")


def _parse_price(a) -> tuple | None:
    if a.price_in is None and a.price_out is None:
        return None
    return (a.price_in or 0.0, a.price_out or 0.0, a.price_cache or 0.0)


def main() -> int:
    ap = argparse.ArgumentParser(description="token 用量 / 前缀缓存命中率聚合")
    ap.add_argument("--dir", default=PROD_TRACE_DIR, help="trace 根目录（默认生产）")
    ap.add_argument("--from", dest="frm", default="", help="起始日 YYYYMMDD（含）")
    ap.add_argument("--to", dest="to", default="", help="结束日 YYYYMMDD（含）")
    ap.add_argument("--days", type=int, default=0, help="只取最近 N 天（0=不限）")
    ap.add_argument("--price-in", type=float, default=None, help="元/百万 token")
    ap.add_argument("--price-out", type=float, default=None, help="元/百万 token")
    ap.add_argument("--price-cache", type=float, default=None, help="元/百万 token")
    a = ap.parse_args()
    price = _parse_price(a)

    paths = iter_trace_files(a.dir)
    keep = []
    for p in paths:
        d = _stamp_of(p)
        if a.frm and d < a.frm:
            continue
        if a.to and d > a.to:
            continue
        keep.append(p)
    if a.days > 0:
        days = sorted({_stamp_of(p) for p in keep})[-a.days:]
        keep = [p for p in keep if _stamp_of(p) in days]

    got = collect(keep)
    agg = got["rows"]
    print("# token 用量 / 前缀缓存命中率")
    print(f"目录：{a.dir}")
    print(f"语料：{got['traces']} 份 trace"
          + (f"（{a.frm or '不限'} ~ {a.to or '不限'}）" if a.frm or a.to else ""))
    if not agg:
        print("\n没有带用量的 llm_done 事件。两种可能，别混为一谈：")
        print("  · 这份语料早于 20260927（记账上线日）⇒ 老 trace 里本来就没有这一格；")
        print("  · 记账没接上 ⇒ 去 tests/test_llm_usage.py 看那条接线判据。")
        return 0

    print("\n## 按天 × 节点 × 引擎")
    print(_HEAD)
    for k in sorted(agg):
        print(_row(f"{k[0]}  {k[1]}/{k[2]}", agg[k], price))

    print("\n## 合计")
    print(_HEAD)
    tot = _sum(agg, list(agg))
    print(_row("全部", tot, price))
    for node in sorted({k[1] for k in agg}):
        print(_row(f"  节点 {node}", _sum(agg, [k for k in agg if k[1] == node]), price))
    for eng in sorted({k[2] for k in agg}):
        print(_row(f"  引擎 {eng}", _sum(agg, [k for k in agg if k[2] == eng]), price))

    if tot["cache_seen"]:
        print(f"\n命中率 = 命中 tok / 输入 tok（只算**报了**缓存字段的 "
              f"{tot['cache_seen']} 次调用；未量到 {tot['calls'] - tot['cache_seen']} 次"
              f"分母里没有它们）")
    else:
        print("\n⚠️ 一次调用都没报缓存字段 ⇒ **量不到命中率**（不是「没命中」）："
              "先看端点回不回 prompt_tokens_details.cached_tokens。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
