# -*- coding: utf-8 -*-
"""真实对话效率指标（20260902 用户指出"重试次数成本是重要指标"后固化）。

golden 只断言最终文本——首轮零工具+编造+REVISE 修正照样 PASS，能力退化与
打回成本都不可见。本脚本扫 logs/agent/traces/ 真实对话，按技能统计：
  - 首轮即调率（rounds[0] 非空 = 第一次 LLM 调用就输出 tool_calls）
  - REVISE/打回率与次数（一次 REVISE = 一整轮 executor 重生成，2× token + 一轮延迟）
  - planner 点名工具 vs 自由 ReAct 的对比（content_query 两种形态）

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
import gzip
import json
import os
import re
import sys
from collections import defaultdict

# trace 文件枚举的唯一实现（20260925：生产 trace 改按天分目录，四种读取端共用一处，
# 免得"改了布局漏改一个脚本"= 那天它少看一半数据）。只吃路径参数、不依赖应用配置。
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from trace_files import iter_trace_files  # noqa: E402

TRACE_DIR = "/home/ubuntu/memory_blog_rust/logs/agent/traces"


def load_trace(path: str) -> dict | None:
    try:
        if path.endswith(".gz"):
            return json.loads(gzip.decompress(open(path, "rb").read()))
        return json.load(open(path))
    except Exception:
        return None


def analyze(d: dict) -> dict | None:
    """返回 {skill, ptools, rounds, rev} 或 None（无完整决策/回复的 trace）。"""
    evs = d.get("events", [])
    skill = ptools = None
    rounds, cur_calls, rev = [], set(), 0
    for e in evs:
        if e.get("node") == "planner" and e.get("event") == "decision":
            skill = e.get("skill")
            ptools = e.get("tools") or []
        if e.get("tool_calls"):
            for tc in e["tool_calls"]:
                cur_calls.add(tc.get("name") if isinstance(tc, dict) else str(tc))
        if e.get("event") == "check":
            rounds.append(sorted(cur_calls))
            cur_calls = set()
            if e.get("done") is False:
                rev += 1
    if not rounds or not skill or not (d.get("reply") or "").strip():
        return None
    return {"skill": skill, "ptools": ptools, "rounds": rounds, "rev": rev}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="since", default="", help="起点日期 20260901")
    ap.add_argument("--to", dest="until", default="99999999", help="终点 20260901T2200")
    ap.add_argument("--json", action="store_true", help="JSON 输出（对账/存档用）")
    args = ap.parse_args()

    stat = defaultdict(lambda: [0, 0, 0, 0, 0])  # 总, 首轮即调, 首轮零工具, 打回过, 打回总次数
    detail = []
    files = iter_trace_files(TRACE_DIR)
    for f in files:
        m = re.search(r"(20\d{6})T(\d{6})_(\d+)_", f)  # 文件名 HHMMSS 6 位 + userid，三组捕获
        if not m:
            continue
        stamp = m.group(1) + "T" + m.group(2)
        if not (args.since <= stamp <= args.until):
            continue
        d = load_trace(f)
        if d is None:
            continue
        a = analyze(d)
        if a is None:
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
        detail.append({"stamp": stamp, "uid": m.group(3), "skill": a["skill"],
                       "ptools": a["ptools"], "rounds": a["rounds"], "rev": a["rev"]})

    if args.json:
        out = {"window": [args.since or "begin", args.until], "skills": {}, "detail": detail}
        for k, v in stat.items():
            out["skills"][k] = dict(zip(
                ["total", "first_round_tools", "first_round_zero", "cases_revised", "rev_total"], v))
        print(json.dumps(out, ensure_ascii=False, indent=1))
        return

    print(f"== 真实对话效率指标 [{args.since or '开始'} → {args.until}] ==")
    for k, (tot, ft, fz, rv, rt) in sorted(stat.items(), key=lambda x: -x[1][0]):
        ft_pct = 100 * ft // tot if tot else 0
        print(f"{k:16s} 总={tot:3d}  首轮即调={ft:3d}({ft_pct}%)  首轮零工具={fz:3d}  "
              f"打回对话={rv:3d}({100 * rv // tot if tot else 0}%)  打回总次数={rt}")
    revised = [x for x in detail if x["rev"]]
    if revised:
        print("\n打回过明细:")
        for x in sorted(revised, key=lambda x: x["stamp"]):
            print(f"  {x['stamp']} u{x['uid']} [{x['skill']}] planner_tools={x['ptools']} "
                  f"rev={x['rev']} rounds={x['rounds']}")


if __name__ == "__main__":
    main()
