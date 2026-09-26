# -*- coding: utf-8 -*-
"""真实对话效率指标（20260902 用户指出"重试次数成本是重要指标"后固化）。

golden 只断言最终文本——首轮零工具+编造+修正照样 PASS，能力退化与
打回成本都不可见。本脚本扫 logs/agent/traces/ 真实对话，按技能统计：
  - 首轮即调率（round 0 的计划就点了工具名，而不是空手交给 narrator）
  - 打回成本：gate fallback 次数（整轮被否决，最贵）+ 受阻 spec 数（execute BLOCK）
  - planner 点名工具 vs 自由计划的对比（content_query 两种形态）

**20260926 修正一处静默假绿**（本脚本此前连续 23 天在骗人）：`analyze` 读的两处键
（`check` 事件上的 `tool_calls`、`done`）自 20260904 receipt 驱动重构起就再没命中过——
事件形状早已改成「每个 spec 一条 `execute/check`（`tool`/`verdict`/`reason`/`skill`）+
`planner/decision` 自带 `round`」，而 `tool_calls` 如今只出现在 `model/llm_done` 上且恒假。
键读不到**不报错**，于是全表打印「首轮即调=0% / 打回=0」看起来像"效率很好"。
⇒ 现在按真实键取，且**读不到信号的 trace 一律返回 None**（计入输出里的"未识别"），
宁可说"这份没读到"，也不许把"没读到"渲染成"确实是 0"。

用法（cd saudade-blog-agent）：
  .venv/bin/python eval/trace_metrics.py                 # 默认近 24h
  .venv/bin/python eval/trace_metrics.py --from 20260901 # 自该日起
  .venv/bin/python eval/trace_metrics.py --from 20260830 --to 20260901T2200  # 历史窗口

技能形态对照（数据解读用）：
  rag_query（0901 前）= 受限规划：TOOLS 固定两段式（rag_search+get_article_detail），
    首轮 100% 即调但 REVISE 多为"读错文章/参数填错"的策略性打回
  content_query 点名 = planner 显式点名工具（20260902 起，_EXPLICIT_TOOLS）
  content_query 自由 = 自由 ReAct（plan=[]），零工具豁免口子在规则 1
"""
import argparse
import json
import os
import sys
from collections import defaultdict

# trace 文件枚举的唯一实现（20260925：生产 trace 改按天分目录，四种读取端共用一处，
# 免得"改了布局漏改一个脚本"= 那天它少看一半数据）。只吃路径参数、不依赖应用配置。
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from trace_files import iter_trace_files, parse_trace_name  # noqa: E402
from trace_io import load_trace  # noqa: E402  （读取的唯一实现，含 gz）

TRACE_DIR = "/home/ubuntu/memory_blog_rust/logs/agent/traces"


def _call_names(tools: list) -> set:
    """TOOLS 行的 `name({...})` → 裸工具名集合（`get_article_detail(定位到的文章id)` 也认）。"""
    out = set()
    for t in tools:
        s = str(t)
        out.add(s.split("(", 1)[0].strip() if "(" in s else s.strip())
    return out


def analyze(d: dict) -> dict | None:
    """返回 {skill, ptools, rounds, rev, blocked} 或 None（**读不到信号**的 trace）。

    None 的两种含义都算"这份没读到"：没有计划事件、或没有回复正文。调用端必须把它
    计进"未识别"并印出来——**不许当成"这条确实是零"**。
    """
    evs = d.get("events", [])
    plans = [e for e in evs
             if (e.get("node"), e.get("event")) in (("planner", "decision"), ("planner", "fastpath"))]
    decs = [e for e in plans if e.get("event") == "decision"]
    checks = [e for e in evs if (e.get("node"), e.get("event")) == ("execute", "check")]
    if not decs or not (d.get("reply") or "").strip():
        return None
    # 按轮聚合计划点名的工具（fastpath 与 decision 同轮出现时取并集；旧 trace 无 round 归 0）
    by_round: dict = {}
    for e in plans:
        r = e.get("round")
        by_round.setdefault(0 if r is None else r, set()).update(_call_names(e.get("tools") or []))
    rounds = [sorted(by_round[r]) for r in sorted(by_round)]
    fallbacks = [e for e in evs if (e.get("node"), e.get("event")) == ("gate", "fallback")]
    return {"skill": decs[-1].get("skill"), "ptools": decs[-1].get("tools") or [],
            "rounds": rounds, "rev": len(fallbacks),
            "blocked": sum(1 for e in checks if e.get("verdict") == "BLOCK")}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="since", default="", help="起点日期 20260901")
    ap.add_argument("--to", dest="until", default="99999999", help="终点 20260901T2200")
    ap.add_argument("--json", action="store_true", help="JSON 输出（对账/存档用）")
    args = ap.parse_args()

    stat = defaultdict(lambda: [0, 0, 0, 0, 0, 0])  # 总, 首轮即调, 首轮零工具, 有打回, 打回次数, 受阻 spec
    detail = []
    unread = {"no_file": 0, "no_signal": 0}   # 读不到的两类，必须印出来（不许当 0）
    files = iter_trace_files(TRACE_DIR)
    for f in files:
        stamp, uid_s = parse_trace_name(f)   # 文件名 HHMMSS 6 位 + userid，一次取两组
        if not stamp or not (args.since <= stamp <= args.until):
            continue
        d = load_trace(f)
        if d is None:
            unread["no_file"] += 1
            continue
        a = analyze(d)
        if a is None:
            unread["no_signal"] += 1
            continue
        s = stat[a["skill"]]
        s[0] += 1
        if a["rounds"][0]:
            s[1] += 1
        else:
            s[2] += 1
        if a["rev"]:
            s[3] += 1
        s[4] += a["rev"]
        s[5] += a["blocked"]
        detail.append({"stamp": stamp, "uid": uid_s, "skill": a["skill"],
                       "ptools": a["ptools"], "rounds": a["rounds"], "rev": a["rev"],
                       "blocked": a["blocked"]})

    if args.json:
        out = {"window": [args.since or "begin", args.until], "unread": unread,
               "skills": {}, "detail": detail}
        for k, v in stat.items():
            out["skills"][k] = dict(zip(
                ["total", "first_round_tools", "first_round_zero", "cases_fallback",
                 "fallback_total", "blocked_total"], v))
        print(json.dumps(out, ensure_ascii=False, indent=1))
        return

    print(f"== 真实对话效率指标 [{args.since or '开始'} → {args.until}] ==")
    for k, (tot, ft, fz, rv, rt, bk) in sorted(stat.items(), key=lambda x: -x[1][0]):
        ft_pct = 100 * ft // tot if tot else 0
        print(f"{k:16s} 总={tot:3d}  首轮即调={ft:3d}({ft_pct}%)  首轮零工具={fz:3d}  "
              f"被否决对话={rv:3d}({100 * rv // tot if tot else 0}%)  否决次数={rt}  受阻spec={bk}")
    print(f"\n未识别 {unread['no_file'] + unread['no_signal']} 条"
          f"（坏文件 {unread['no_file']} / 无计划或无回复 {unread['no_signal']}）"
          f"——**未识别不是 0**，窗口内总文件数={len(files)}")
    revised = [x for x in detail if x["rev"]]
    if revised:
        print("\n被 gate 否决过的对话:")
        for x in sorted(revised, key=lambda x: x["stamp"]):
            print(f"  {x['stamp']} u{x['uid']} [{x['skill']}] planner_tools={x['ptools']} "
                  f"rev={x['rev']} blocked={x['blocked']} rounds={x['rounds']}")


if __name__ == "__main__":
    main()
