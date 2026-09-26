# -*- coding: utf-8 -*-
"""全量 trace 语料的结构不变量（20260926 · 判据基座）。

**为什么要有这个脚本**：此前每问一次"全量语料里有多少条 X"，答案都是当场敲一段
heredoc——结论不可复核、口径每次都不一样，而且**同一段扫描重写三遍就有三个版本**。
最贵的一次：裸 `json.load` 读"948 份 trace"实际只读到 86 份（862 份 `.gz` 静默抛异常
被跳过），真值 3 条被报成 1 条。所以把扫描固定成四条**可复跑**的不变量，语料一律走
`trace_files.iter_trace_files` + `trace_io.load_trace`（枚举与读取各只有一份实现）。

**判据是"变了没有"，不是"判谁有罪"**：每条不变量 = 一个计数 + 一份带时间戳的清单，
清单逐条印现场供人分类，脚本不替人下结论（元讨论引述与真事故在字面上长得一样，
机器分不干净——这正是它只列不判的理由）。要盯"有没有新增"，用 `--compare` 比基线。

四条不变量：
  I1 `reply_cmd_prefix`        终稿正文里出现命令前缀标签
                               （`AUTO_NAVIGATE:` / `NAVIGATE:` / `EFFECT:` / `DARKMODE:`）
                               按"本轮有没有真命令回执"分两型——
                               `cited`（有回执）= 引用回执时把标签一起抄进正文；`invented`
                               （无回执）= 模型自己在正文里写标签，含合法的元讨论引述，
                               只列不设目标。
                               **`cited` 只能在"改动上线之后的新窗口"里读 0**（`--from <上线日>`）：
                               存量 trace 是**历史事实**，代码改了不会让昨天那份 trace 里的
                               标签消失，它随保留期自然出清。拿全量语料的 `cited` 当"改完是不是
                               好了"的判据，会得到"改了也没变"的假结论——这正是本脚本头注
                               "判据是变了没有、不是判谁有罪"的那一层。
  I2 `zero_tool_plan_to_narrator`  末轮计划是非 chat 技能、工具清单为空，且**整个 trace 一次
                               执行都没有**——即"没有执行过任何东西的计划被交给了 narrator"。
                               ⚠️ 这条口径**宽于**它要抓的缺陷：通用知识问答（合法）、如实说
                               "我查不到/做不到"的收尾轮都落在里面（存量 11 条里多数是这类）。
                               缺陷子集是"系统确定性层**明知**这一轮零执行、却照原样把计划交给
                               narrator 自由发挥"——那需要 `plan.status` 才判得准（批 3），
                               届时把这条收窄成子集，别拿现在的计数当"11 条事故"。
  I3 `fallback_by_issue`       gate fallback 按原因码计数（`cmd_prefix` 是批 2 的目标）。
  I4 `fallback_total`          gate fallback 总数 = 生产侧 `__RESET__` 帧数。
                               **`0` 有强含义**：这个窗口里 gate 那条路一次都没被验到，
                               别读成"系统很干净"（golden 侧 `resets=0` 的同一条纪律）。

用法（cd saudade-blog-agent）：
  .venv/bin/python eval/corpus_invariants.py                    # 全量语料，只列不写
  .venv/bin/python eval/corpus_invariants.py --from 20260920
  .venv/bin/python eval/corpus_invariants.py --json
  .venv/bin/python eval/corpus_invariants.py --baseline         # 快照落 eval/report/（不进 git）
  .venv/bin/python eval/corpus_invariants.py --compare eval/report/corpus_invariants_xxx.json

退出码：默认恒 0（非门禁）；只有 `--compare` 发现**基线里没有的**新命中才返回 1。
"""
import argparse
import json
import os
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime

# 枚举与读取各只有一份实现（20260925 布局改按天、20260926 补 gz）——别在这里内联 glob/json
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from trace_files import iter_trace_files, parse_trace_name  # noqa: E402
from trace_io import load_trace  # noqa: E402
# 连线命令的**唯一** Python 侧实现（命令 → `AUTO_NAVIGATE:<url>` 等连线形）。
# 这里刻意不复写一份：批 2 之后 trace 里的命令是结构化 `cmd`，与 golden/server 那边
# 重建出来的是同一件东西，各写一份就是漂移（同 `_cmd_wire` 的文档字符串）。
from agent.graph import _cmd_wire  # noqa: E402

TRACE_DIR = "/home/ubuntu/memory_blog_rust/logs/agent/traces"
REPORT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "report")

# 连线命令前缀（回复侧判据那三根正则同族，但这里判的是**终稿正文**里有没有它）
CMD_PREFIX = re.compile(r"AUTO_NAVIGATE:|NAVIGATE:|EFFECT:|DARKMODE:")

# 执行类事件（命令回执就藏在它们的 result 里）；旧图里工具执行挂在 `tools` 节点上，
# 20260903 之后统一到 `execute`——两个都收，免得老窗口的统计凭空少一截。
CALL_EVENTS = (("execute", "call"), ("tools", "call"))


def _ev(d: dict, node: str, event: str) -> list:
    return [e for e in d.get("events", [])
            if e.get("node") == node and e.get("event") == event]


def scan_one(d: dict) -> dict:
    """单份 trace 的命中（返回 {i1: [...], i2: [...], i3: [...], i4: n}）。"""
    out = {"i1": [], "i2": [], "i3": [], "i4": 0}
    evs = d.get("events", [])

    # ── I1 终稿正文里的命令前缀 ────────────────────────────────────────────
    reply = str(d.get("reply") or "")
    toks = {m.group(0) for m in CMD_PREFIX.finditer(reply)}
    if toks:
        # "有回执"的两处来源：① 20260926 批 2 之前的工具返回文本（`result` 里带前缀）；
        # ② 批 2 之后命令搬上了 `cmd` 字段（帧文本已无前缀）——两处都收，这条不变量
        # 才跨得过那次改动（只认 ① 的话，新窗口里"引用回执"永远判不出来）。
        results = [str(e.get("result") or "") for e in evs
                   if (e.get("node"), e.get("event")) in CALL_EVENTS]
        results += [_cmd_wire(e.get("cmd")) for e in evs
                    if (e.get("node"), e.get("event")) in CALL_EVENTS]
        cited = any(t in r for t in toks for r in results)
        # 命中处前后各取 30 字，印出来才分得清"引用回执"和"解释机制"
        at = CMD_PREFIX.search(reply)
        out["i1"].append({"kind": "cited" if cited else "invented",
                          "tokens": sorted(toks), "receipts": len(results),
                          "at": reply[max(0, at.start() - 30):at.end() + 30].replace("\n", " ")})

    # ── I2 零执行的计划交给了 narrator ─────────────────────────────────────
    decs = _ev(d, "planner", "decision")
    calls = [e for e in evs if (e.get("node"), e.get("event")) in CALL_EVENTS]
    if decs:
        last = decs[-1]
        if last.get("skill") != "chat" and not (last.get("tools") or []) and not calls:
            out["i2"].append({"skill": last.get("skill"), "round": last.get("round"),
                              "plan_rounds": len(decs),
                              "reply": str(reply)[:70].replace("\n", " ")})

    # ── I3 / I4 gate fallback ─────────────────────────────────────────────
    fbs = _ev(d, "gate", "fallback")
    out["i3"] = [{"issue": e.get("issue"), "skill": e.get("skill"),
                  "clause": str(e.get("clause") or "")[:80].replace("\n", " ")} for e in fbs]
    out["i4"] = len(fbs)
    return out


def scan(since: str, until: str) -> dict:
    """扫窗口内全部 trace，返回 {window, files, unread, counts, hits}。"""
    counts = Counter()
    hits = defaultdict(list)          # 分类 → [{stamp, uid, ...}, …]
    unread = Counter()
    files = iter_trace_files(TRACE_DIR)
    n_win = 0
    for f in files:
        stamp, uid = parse_trace_name(f)
        if not stamp or not (since <= stamp <= until):
            continue
        n_win += 1
        d = load_trace(f)
        if d is None:
            unread["bad_file"] += 1     # 坏文件 / 读不出——**不是"里面没有那件事"**
            continue
        one = scan_one(d)
        counts["i1_cited"] += sum(1 for x in one["i1"] if x["kind"] == "cited")
        counts["i1_invented"] += sum(1 for x in one["i1"] if x["kind"] == "invented")
        counts["i2"] += len(one["i2"])
        for x in one["i3"]:
            counts[f"i3_{x['issue']}"] += 1
        counts["i4"] += one["i4"]
        for x in one["i1"]:
            hits[f"i1_{x['kind']}"].append({"stamp": stamp, "uid": uid, **x})
        for x in one["i2"]:
            hits["i2"].append({"stamp": stamp, "uid": uid, **x})
        for x in one["i3"]:
            hits[f"i3_{x['issue']}"].append({"stamp": stamp, "uid": uid, **x})
    return {"window": [since or "begin", until], "files": len(files), "in_window": n_win,
            "unread": dict(unread), "counts": dict(counts), "hits": {k: v for k, v in hits.items()}}


def report(r: dict) -> None:
    c = r["counts"]
    print(f"== trace 语料不变量 [{r['window'][0]} → {r['window'][1]}] ==")
    print(f"语料 {r['files']} 份文件，窗口内 {r['in_window']} 份"
          f"（读不出 {r['unread'].get('bad_file', 0)} 份——读不出不是「没有」，见脚本头注）\n")
    print(f"I1 正文含命令前缀   引用回执(cited)={c.get('i1_cited', 0)}  "
          f"自己写的(invented)={c.get('i1_invented', 0)}   ← 改完只看新窗口（--from），"
          f"存量是历史、会自然出清")
    print(f"I2 零执行的计划交给 narrator = {c.get('i2', 0)}")
    print(f"I4 gate fallback 总数（= 生产侧 __RESET__ 帧数）= {c.get('i4', 0)}"
          f"{'   ← 0 表示这条路这次没被验到，不是「干净」' if not c.get('i4') else ''}")
    i3 = sorted((k[3:], v) for k, v in c.items() if k.startswith("i3_"))
    print("I3 按原因码：" + ("  ".join(f"{k}={v}" for k, v in i3) if i3 else "（无）"))
    for cat in sorted(r["hits"]):
        rows = r["hits"][cat]
        print(f"\n── {cat}（{len(rows)} 条）" + ("  ★ 批 2 的目标清单" if cat == "i1_cited" else ""))
        for x in sorted(rows, key=lambda x: x["stamp"]):
            tail = x.get("clause") or x.get("reply") or x.get("at") or ""
            extra = f" issue={x['issue']}" if x.get("issue") else ""
            extra += f" skill={x['skill']}" if x.get("skill") else ""
            extra += f" receipts={x['receipts']}" if "receipts" in x else ""
            print(f"  {x['stamp']} u{x['uid']}{extra}  {tail}")


def main() -> int:
    ap = argparse.ArgumentParser(description="trace 全量语料的结构不变量")
    ap.add_argument("--from", dest="since", default="", help="起点日期 20260920")
    ap.add_argument("--to", dest="until", default="99999999", help="终点 20260926T235959")
    ap.add_argument("--json", action="store_true", help="只输出 JSON（存档/对账用）")
    ap.add_argument("--baseline", action="store_true", help="把本次快照写到 eval/report/")
    ap.add_argument("--compare", default="", help="与既有快照比，只报基线里没有的新命中")
    args = ap.parse_args()

    r = scan(args.since, args.until)

    if args.json:
        print(json.dumps(r, ensure_ascii=False, indent=1))
    else:
        report(r)

    if args.baseline:
        os.makedirs(REPORT_DIR, exist_ok=True)
        p = os.path.join(REPORT_DIR,
                         f"corpus_invariants_{datetime.now().strftime('%Y%m%dT%H%M%S')}.json")
        with open(p, "w", encoding="utf-8") as fh:
            json.dump(r, fh, ensure_ascii=False, indent=1)
        print(f"\n快照已写 {p}")

    if args.compare:
        with open(args.compare, encoding="utf-8") as fh:
            base = json.load(fh)
        known = {(k, x["stamp"], x.get("issue", ""), x.get("kind", ""))
                 for k, rows in base.get("hits", {}).items() for x in rows}
        fresh = []
        for k, rows in r["hits"].items():
            for x in rows:
                key = (k, x["stamp"], x.get("issue", ""), x.get("kind", ""))
                if key not in known:
                    fresh.append((k, x))
        print(f"\n== 与基线对比（{args.compare}）==")
        if not fresh:
            print("✅ 无新增命中")
            return 0
        print(f"❌ 新增 {len(fresh)} 条：")
        for k, x in sorted(fresh, key=lambda y: y[1]["stamp"]):
            print(f"  [{k}] {x['stamp']} u{x['uid']} "
                  f"{x.get('clause') or x.get('reply') or x.get('at') or ''}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
