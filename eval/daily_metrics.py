#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Daily request accounting plus explicitly scoped legacy graph diagnostics.

No model calls are made. Missing usage remains unknown, and production,
evaluation, runtime builds and executed paths remain separate in JSON output.
"""

import argparse
import json
import os
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import token_cost_report as tcr  # noqa: E402
import trace_metrics as tm  # noqa: E402

from eval import request_metrics
from utils.trace_files import iter_trace_files, parse_trace_name  # noqa: E402
from utils.trace_io import load_trace  # noqa: E402


def executed_calls(evs: list) -> list[dict]:
    """`execute/call` 与它后面那条 `execute/check` 配对，标出是否被拦下 / 零改动。

    返回 `[{"tool", "args", "verdict", "noop"}, …]`。没有配对到 `check` 的调用
    照旧带出去（`verdict=None`）——**"没读到回执"不是"成功"**，调用端按它自己的
    口径处置，本函数不替它猜。
    """
    out: list[dict] = []
    pending: dict | None = None
    for e in evs:
        if not isinstance(e, dict) or e.get("node") != "execute":
            continue
        kind = e.get("event")
        if kind == "call":
            if pending is not None:
                out.append(pending)
            pending = {"tool": e.get("name"), "args": e.get("args"),
                       "verdict": None, "noop": False}
        elif kind == "noop" and pending is not None:
            pending["noop"] = True
        elif kind == "check" and pending is not None:
            pending["verdict"] = e.get("verdict")
            out.append(pending)
            pending = None
    if pending is not None:
        out.append(pending)
    return out


def _sig(c: dict) -> tuple:
    return (c.get("tool"), json.dumps(c.get("args"), sort_keys=True, ensure_ascii=False))


def dup_writes(evs: list) -> list[tuple]:
    """这一轮里**成功执行了 ≥2 次**的 (工具名, 参数) ，仅适用于旧图 execute/check 事件。"""
    seen = defaultdict(int)
    for c in executed_calls(evs):
        if c["verdict"] == "PASS" and not c["noop"]:
            seen[_sig(c)] += 1
    return [(t, a, n) for (t, a), n in seen.items() if n >= 2]


def collect(paths: list) -> dict:
    """扫一遍语料，出这一行要的全部数字。"""
    conv = {"total": 0, "unread": 0, "zero_first": 0, "zero_first_chat": 0,
            "rev": 0, "dup": 0, "dup_detail": [], "graph": 0, "hybrid": 0, "handoff": 0}
    per_workflow = defaultdict(lambda: [0, 0])      # [总, 有 gate 否决]
    for p in paths:
        doc = load_trace(p)
        if not doc:
            conv["unread"] += 1
            continue
        conv["total"] += 1
        path = request_metrics.path_of(doc)
        if path == "hybrid":
            conv["hybrid"] += 1
            continue
        if path == "hybrid_to_graph":
            conv["handoff"] += 1
        a = tm.analyze(doc)
        if a is None:
            conv["unread"] += 1
            continue
        conv["graph"] += 1
        if a["rounds"][0]:
            pass
        else:
            conv["zero_first"] += 1
            if a["workflow"] == "chat":
                conv["zero_first_chat"] += 1
        if a["rev"]:
            conv["rev"] += 1
        s = per_workflow[a["workflow"]]
        s[0] += 1
        if a["rev"]:
            s[1] += 1
        dups = dup_writes(doc.get("events") or [])
        if dups:
            conv["dup"] += 1
            conv["dup_detail"].append((a["workflow"], dups))
    conv["per_workflow"] = per_workflow
    return conv


def _m(n: int) -> str:
    """token 数读成人话（一位小数，够看趋势）。"""
    return f"{n / 1e6:.2f}M"


def _inference_by_day(agg: dict) -> dict:
    """按天聚合 所有推理节点的输入/命中（命中率的分母是输入 tok，见头注）。"""
    per = defaultdict(lambda: {"in": 0, "cache": 0, "seen": 0, "calls": 0, "cache_in": 0})
    for (day, node, _eng), r in agg.items():
        d = per[day]
        d["in"] += r["in"]
        d["cache"] += r["cache"]
        d["seen"] += r["cache_seen"]
        d["calls"] += r["calls"]
        d["cache_in"] += r["cache_in"]
    return per


def _pct(a: float, b: float) -> str:
    return f"{a / b * 100:.1f}%" if b else "n/a"


def render(paths: list, days: int) -> str:
    conv = collect(paths)
    tot = tcr.totals(paths)
    per_day = _inference_by_day(tcr.collect(paths)["rows"])

    parts = [f"近{days}天 traces={conv['total']} 未识别={conv['unread']}"]
    parts.append(f"hybrid={conv['hybrid']} 交接={conv['handoff']}")
    if conv["graph"]:
        zf = conv["zero_first"]
        parts.append(f"graph首轮零工具={zf}/{conv['graph']}（chat {conv['zero_first_chat']}）")
        parts.append(f"graph否决={conv['rev']}/{conv['graph']}")
    dup_txt = f"graph写重复={conv['dup']}"
    if conv["dup_detail"]:
        # 重复的**是哪一件**比次数重要：同一个工具在一轮里执行 3 次和 3 个工具各 2 次
        # 是两种病（前者是循环，后者是多步任务里的撞车）。
        items = []
        for _workflow, dups in conv["dup_detail"][:3]:
            items += [f"{t}×{n}" for t, _a, n in dups]
        dup_txt += "（" + "、".join(items[:3]) + "）"
    parts.append(dup_txt)

    # 按技能的**异常聚集**：单条 gate 否决是噪声，同一技能成片才是信号
    # （现场：account_roster 3/18 全在 10-12～10-14 三天，形态一模一样）。
    hot = [f"{k}({v[1]}/{v[0]})" for k, v in sorted(conv["per_workflow"].items())
           if v[0] >= 3 and v[1] / v[0] >= 0.3]
    if hot:
        parts.append("异常技能=" + "、".join(hot[:3]))

    if per_day:
        last = max(per_day)
        d = per_day[last]
        cache = f"模型缓存={_pct(d['cache'], d['cache_in'])}"
        prev = [x for x in sorted(per_day) if x < last]
        if prev:
            q = per_day[prev[-1]]
            cache += f"（{prev[-1][4:]} {_pct(q['cache'], q['cache_in'])}）"
        parts.append(f"末日{last[4:]} {cache}")
    else:
        parts.append("模型缓存=量不到")
    parts.append(f"已测输入tok={_m(tot['in'])}（{tot['measured']}/{tot['calls']}调用）")
    if tot["cache_seen"] < tot["calls"]:
        parts.append(f"⚠️报缓存{tot['cache_seen']}/{tot['calls']}")
    return " | ".join(parts)


def main() -> int:
    ap = argparse.ArgumentParser(description="每日零成本观测一行")
    ap.add_argument("--dir", default=tcr.PROD_TRACE_DIR, help="trace 根目录（默认生产）")
    ap.add_argument("--days", type=int, default=7, help="窗口天数（按 trace 日期）")
    ap.add_argument("--json", action="store_true", help="按来源、版本、模型、路径分组的请求指标")
    a = ap.parse_args()

    paths = iter_trace_files(a.dir)
    if a.days > 0:
        stamps = sorted({(parse_trace_name(p)[0] or "")[:8] for p in paths} - {""})
        keep = set(stamps[-a.days:])
        paths = [p for p in paths if (parse_trace_name(p)[0] or "")[:8] in keep]
    print(json.dumps(request_metrics.collect(paths), ensure_ascii=False) if a.json
          else render(paths, a.days))
    return 0


if __name__ == "__main__":
    sys.exit(main())
