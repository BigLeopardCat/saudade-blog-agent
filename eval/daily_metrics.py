#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""每日零成本观测**一行**（20261016）。

**为什么存在**：20261012 起夜间回归整体挂起（主人「钱包扛不住」），而
`scripts/nightly_regression.sh` 把 L1/L2/L3 + `trace_alert` + `golden_draft` 装在
**同一个脚本**里 ⇒ 一停全停，生产健康只剩 `scripts/healthcheck.sh` 每分钟那八项。
`eval/token_cost_report.py`（成本 / 前缀缓存）与 `eval/trace_metrics.py`（真实流量效率）
这两件**零 LLM 成本**的读数装置从此没有任何自动通道——只有人想起来才跑。代价有现场：
20261016 planner 的前缀缓存命中率从 84.6% 掉到 51.8%，这件事此前**没有任何东西会报**，
是偶然手跑才看见的。

⚠️ **初稿把原因写成"当天改了提示词"，当天复盘更正为误判**（本条留在这里，因为它是
"看着像因果、其实没验"的现场）：那天 17 条 trace 全落在 02:29–04:24，而
`agent/prompts.py` 的改动 03:11 才落盘、05:09 才随重启生效（health.log 只有那一次
master 更换）⇒ 当天读数一次都没跑到新提示词。真实原因是**流量稀疏**：两小时窗口里
只有 4 段对话、段间隔 20 分钟以上，前缀缓存在段间冷掉，每次冷启动丢掉整个 ~51k 前缀
（`token_cost_report` 的逐条读数：0 / 2048 / 5120 / 46464 这种阶梯，就是冷启动后
逐次预热的样子）。**判据是"改完有没有重启、重启前后各有几条 trace"，不是"那天改了
什么代码"。**

本脚本把那两件 + 一条新判据（**同工具同参数的写被重复执行**）压成一行，
由 `healthcheck.sh` 第 9 项每天调一次，追加进 `logs/health.log`。
**只写一行是刻意的**：health.log 是"扫一眼找异常"的地方，不是报表——要看细节去跑
那两个脚本本身（它们一个 `--json`、一个按天打表）。

## 判据的口径（都写在这里，别在调用端另算一份）

- **窗口**按 trace 文件名里的日期取（`trace_files.parse_trace_name`，与四个读取端同源）。
- **未识别**照 `trace_metrics.analyze` 的口径（没有计划事件 / 没有回复正文）——
  **它不是 0**，必须印出来（本仓老账：把"没读到"渲染成"确实是 0"）。
- **写重复**＝一轮对话里**同一个 (工具名, 参数) 成功执行了 ≥2 次**。
  ⚠️ 判据是 `execute/call` **配上**它后面那条 `execute/check`，且要求
  `verdict == PASS` **且**中间没有 `execute/noop`：
  · 只数 `call` 会把**被拦下**的调用算进来（实测假阳性：`audit_board_comment`
    连发两次 `consent_required`，一次都没执行）；
  · 不看 `noop` 会把**零净改动**的重复算进来（实测假阳性：`set_account_role`
    第二次是 `changed=False`，`_trim_noop_specs` 已经按设计拦住了）。
  两处都是"尺子不对"，不是数据不对——**这条判据的第一版两个都踩了**。
- **前缀缓存命中率**只算**报了缓存字段**的那些调用（`cache_read` 缺席 ≠ 0，
  见 `agent/llm_usage.py`）；分母是输入 tok 总数，`cache_seen` 只当闸。
  "末日"＝窗口里有用量的**最后一天**（不是"今天"：探针在凌晨跑，"今天"常常还是空的）。

退出码恒 0（同 `token_cost_report.py` / `llm_judge.py`）：它出的是**报表**，
判据在别处；读不出来时它说"读不出来"，不编 0。

用法（cd saudade-blog-agent）：
  .venv/bin/python eval/daily_metrics.py                # 近 7 天，一行
  .venv/bin/python eval/daily_metrics.py --days 1
  .venv/bin/python eval/daily_metrics.py --dir <trace 目录>
"""
import argparse
import json
import os
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
# 枚举与读取各自只有一个实现（20260925/20260926）：布局或压缩形态一变，
# 四个读取端不会各看一半语料。
from trace_files import iter_trace_files, parse_trace_name  # noqa: E402
from trace_io import load_trace  # noqa: E402
import token_cost_report as tcr  # noqa: E402
import trace_metrics as tm  # noqa: E402


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
    """这一轮里**成功执行了 ≥2 次**的 (工具名, 参数) —— 见模块头注的口径。"""
    seen = defaultdict(int)
    for c in executed_calls(evs):
        if c["verdict"] == "PASS" and not c["noop"]:
            seen[_sig(c)] += 1
    return [(t, a, n) for (t, a), n in seen.items() if n >= 2]


def collect(paths: list) -> dict:
    """扫一遍语料，出这一行要的全部数字。"""
    conv = {"total": 0, "unread": 0, "zero_first": 0, "zero_first_chat": 0,
            "rev": 0, "dup": 0, "dup_detail": []}
    per_skill = defaultdict(lambda: [0, 0])      # [总, 有 gate 否决]
    for p in paths:
        doc = load_trace(p)
        if not doc:
            conv["unread"] += 1
            continue
        a = tm.analyze(doc)
        if a is None:
            conv["unread"] += 1
            continue
        conv["total"] += 1
        if a["rounds"][0]:
            pass
        else:
            conv["zero_first"] += 1
            if a["skill"] == "chat":
                conv["zero_first_chat"] += 1
        if a["rev"]:
            conv["rev"] += 1
        s = per_skill[a["skill"]]
        s[0] += 1
        if a["rev"]:
            s[1] += 1
        dups = dup_writes(doc.get("events") or [])
        if dups:
            conv["dup"] += 1
            conv["dup_detail"].append((a["skill"], dups))
    conv["per_skill"] = per_skill
    return conv


def _m(n: int) -> str:
    """token 数读成人话（一位小数，够看趋势）。"""
    return f"{n / 1e6:.2f}M"


def _planner_by_day(agg: dict) -> dict:
    """按天聚合 **planner** 节点的输入/命中（命中率的分母是输入 tok，见头注）。"""
    per = defaultdict(lambda: {"in": 0, "cache": 0, "seen": 0, "calls": 0})
    for (day, node, _eng), r in agg.items():
        if node != "planner":
            continue
        d = per[day]
        d["in"] += r["in"]
        d["cache"] += r["cache"]
        d["seen"] += r["cache_seen"]
        d["calls"] += r["calls"]
    return per


def _pct(a: float, b: float) -> str:
    return f"{a / b * 100:.1f}%" if b else "n/a"


def render(paths: list, days: int) -> str:
    conv = collect(paths)
    tot = tcr.totals(paths)
    per_day = _planner_by_day(tcr.collect(paths)["rows"])

    parts = [f"近{days}天 traces={conv['total']} 未识别={conv['unread']}"]
    if conv["total"]:
        zf = conv["zero_first"]
        parts.append(f"首轮零工具={zf}/{conv['total']}（chat {conv['zero_first_chat']}）")
        parts.append(f"gate否决={conv['rev']}/{conv['total']}")
    dup_txt = f"写重复={conv['dup']}"
    if conv["dup_detail"]:
        # 重复的**是哪一件**比次数重要：同一个工具在一轮里执行 3 次和 3 个工具各 2 次
        # 是两种病（前者是循环，后者是多步任务里的撞车）。
        items = []
        for _skill, dups in conv["dup_detail"][:3]:
            items += [f"{t}×{n}" for t, _a, n in dups]
        dup_txt += "（" + "、".join(items[:3]) + "）"
    parts.append(dup_txt)

    # 按技能的**异常聚集**：单条 gate 否决是噪声，同一技能成片才是信号
    # （现场：account_roster 3/18 全在 10-12～10-14 三天，形态一模一样）。
    hot = [f"{k}({v[1]}/{v[0]})" for k, v in sorted(conv["per_skill"].items())
           if v[0] >= 3 and v[1] / v[0] >= 0.3]
    if hot:
        parts.append("异常技能=" + "、".join(hot[:3]))

    if per_day:
        last = max(per_day)
        d = per_day[last]
        cache = f"planner缓存={_pct(d['cache'], d['in'])}"
        prev = [x for x in sorted(per_day) if x < last]
        if prev:
            q = per_day[prev[-1]]
            cache += f"（{prev[-1][4:]} {_pct(q['cache'], q['in'])}）"
        parts.append(f"末日{last[4:]} {cache}")
    else:
        parts.append("planner缓存=量不到")
    parts.append(f"输入tok={_m(tot['in'])}")
    if tot["cache_seen"] < tot["calls"]:
        # 有调用没报缓存字段 ⇒ 命中率**少算**了那一部分。只在真出现时印
        # （常态不印，免得每天一行都是废话）。
        parts.append(f"⚠️报缓存{tot['cache_seen']}/{tot['calls']}")
    return " | ".join(parts)


def main() -> int:
    ap = argparse.ArgumentParser(description="每日零成本观测一行")
    ap.add_argument("--dir", default=tcr.PROD_TRACE_DIR, help="trace 根目录（默认生产）")
    ap.add_argument("--days", type=int, default=7, help="窗口天数（按 trace 日期）")
    a = ap.parse_args()

    paths = iter_trace_files(a.dir)
    if a.days > 0:
        stamps = sorted({(parse_trace_name(p)[0] or "")[:8] for p in paths} - {""})
        keep = set(stamps[-a.days:])
        paths = [p for p in paths if (parse_trace_name(p)[0] or "")[:8] in keep]
    print(render(paths, a.days))
    return 0


if __name__ == "__main__":
    sys.exit(main())
