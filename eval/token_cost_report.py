#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""按天、节点和引擎聚合已观测用量；缺失字段不作为零值。

价格由调用方完整提供，单位为元/百万 token；用量不完整时不估算费用。
请求来源、运行版本与执行路径的分组报告见 daily_metrics --json。
"""
import argparse
import math
import os
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
# 生产 trace 根 = 应用配置那一处（`settings.trace_dir`），不手抄第二份路径。
from config import settings  # noqa: E402
from eval import usage
from utils.trace_files import iter_trace_files, parse_trace_name  # noqa: E402
from utils.trace_io import load_trace  # noqa: E402

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
    """Group completed inference events and their measured usage by day/node/engine."""
    agg = defaultdict(usage.empty)
    traces = 0
    for p in paths:
        doc = load_trace(p)
        if not isinstance(doc, dict):
            continue
        traces += 1
        day = _stamp_of(p)
        events = defaultdict(list)
        for ev in doc.get("events") or []:
            if isinstance(ev, dict) and ev.get("event") in ("llm_start", "llm_done", "llm_error"):
                events[(day, ev.get("node") or "?", ev.get("engine") or "-")].append(ev)
        for key, values in events.items():
            measured = usage.totals(values)
            for field in measured:
                agg[key][field] += measured[field]
    return {"traces": traces, "rows": agg}


def _sum(agg: dict, keys: list) -> dict:
    out = usage.empty()
    for k in keys:
        for f in out:
            out[f] += agg[k][f]
    return out


def totals(paths: list[str]) -> dict:
    """Aggregate all measured input; cache ratio only uses cache-covered input."""
    got = collect(paths)
    tot = _sum(got["rows"], list(got["rows"]))
    tot["traces"] = got["traces"]
    tot["hit_rate"] = usage.hit_rate(tot)
    return tot


def _row(label: str, r: dict, price: tuple | None) -> str:
    ratio = usage.hit_rate(r)
    hit = f"{ratio * 100:.1f}%" if ratio is not None else "n/a"
    tail = ""
    if price and usage.complete(r):
        p_in, p_out, p_cache = price
        miss = r["in"] - r["cache"]
        yuan = (miss * p_in + r["cache"] * p_cache + r["out"] * p_out) / 1e6
        no_cache = (r["in"] * p_in + r["out"] * p_out) / 1e6
        tail = f" | ¥{yuan:.2f}（不命中则 ¥{no_cache:.2f}）"
    elif price:
        tail = " | 费用未知（用量不完整）"
    return (f"{label:<28} {r['calls']:>5} {r['in']:>10} {r['out']:>7} "
            f"{r['cache']:>10} {hit:>7} {r['calls'] - r['cache_seen']:>5} "
            f"{r['unknown_attempts']:>7}{tail}")


_HEAD = (f"{'分组':<28} {'调用':>5} {'输入tok':>10} {'输出tok':>7} "
         f"{'命中tok':>10} {'命中率':>7} {'未量到':>5} {'未知尝试':>7}")


def _parse_price(a) -> tuple | None:
    values = (a.price_in, a.price_out, a.price_cache)
    if all(v is None for v in values):
        return None
    if any(v is None or not math.isfinite(v) or v < 0 for v in values):
        raise ValueError("请完整提供三个有限且非负的单价")
    return values


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
    try:
        price = _parse_price(a)
    except ValueError as exc:
        ap.error(str(exc))

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
        print("\n没有完成的模型调用事件；不代表请求成功或零消耗。")
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

    print(f"\n字段覆盖：输入 {tot['measured']}/{tot['calls']}，"
          f"输出 {tot['output_seen']}/{tot['calls']}，"
          f"缓存 {tot['cache_seen']}/{tot['calls']}，"
          f"思考 {tot['reasoning_seen']}/{tot['calls']}")
    if tot["unknown_attempts"]:
        print(f"\n另有 {tot['unknown_attempts']} 次失败或未收尾调用，用量未知；"
              "已测 token 不能视为提供商全账单。")
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
