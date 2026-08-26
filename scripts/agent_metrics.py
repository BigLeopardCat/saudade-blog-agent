#!/usr/bin/env python3
"""Agent 质量代理指标统计（可观测最小集，无新依赖）。

从 agent 日志量化 eval-observability.md §5.2 的故障模式信号：
空回复率 / REVISE 率 / 工具失败率 / 命令帧率 / 摘要失败率 / 超时次数。

用法：
  python scripts/agent_metrics.py                 # 最近 24h，文本表格
  python scripts/agent_metrics.py --hours 168     # 最近 7 天
  python scripts/agent_metrics.py --json          # JSON 输出（供 cron/脚本消费）
  python scripts/agent_metrics.py --log PATH      # 指定日志文件（默认 /tmp/agent_server.log）

约定：日志行格式 `%(asctime)s | %(name)s | %(levelname)s | tid=%(trace_id)s | %(message)s`，
时间戳为前 19 字符（YYYY-MM-DD HH:MM:SS）。trace_id 透传后同一次对话的
planner/model/tools/reflector 日志共享同一 tid——可用 --tid 抽出一条对话的完整轨迹。
"""

import argparse
import datetime as dt
import json
import re
import sys

# ── 日志模式 → 指标 ───────────────────────────────────────────────
RE_TIMESTAMP = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})")
RE_TRACE_ID = re.compile(r"tid=(\S+)")
RE_PLANNER = re.compile(r"\[planner\] skill=(\w+)")
RE_TOOL_CALLS = re.compile(r"\[model\] tool_calls=\[([^\]]*)\]")
RE_TOOLS = re.compile(r"\[tools\] (\w+) → (.+)")
RE_COMMAND_FRAME = re.compile(r"^(EFFECT|NAVIGATE|AUTO_NAVIGATE|DARKMODE):")
RE_ERROR_FRAME = re.compile(r"^(__ERROR__|无效|失败)")
RE_DEDUP = re.compile(r"该内容刚刚已由系统执行显示")

REFLECTOR_PASS = ("[reflector] PASS", "PASS")
REFLECTOR_REVISE = ("[reflector]", "REVISE")
RE_BUDGET = re.compile(r"反思预算耗尽")
RE_EMPTY = re.compile(r"(Agent returned empty reply|ended with no output)")
RE_SUMMARY_FAIL = re.compile(r"独立摘要生成失败")
RE_TIMEOUT = re.compile(r"(total timeout|timeout \(idle/total)")
RE_AGENT_ERR = re.compile(r"Agent (invocation|streaming) failed")
RE_DISPLAY_FAST = re.compile(r"设备显示注记快道")


def parse_line(line: str) -> tuple | None:
    m = RE_TIMESTAMP.match(line)
    if not m:
        return None
    ts = dt.datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")
    return ts, line


def collect(path: str, hours: int) -> dict:
    cutoff = dt.datetime.now() - dt.timedelta(hours=hours)
    stats = {
        "rounds": 0, "skills": {}, "tool_call_rounds": 0, "tool_call_total": 0,
        "tool_failures": 0, "command_frames": 0, "dedup_blocks": 0,
        "reflector_pass": 0, "reflector_revise": 0, "budget_exhausted": 0,
        "empty_reply": 0, "summary_failed": 0, "timeouts": 0,
        "agent_errors": 0, "display_fastpath": 0,
        "trace_ids": set(), "window_start": None, "window_end": None,
    }
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            parsed = parse_line(line)
            if not parsed:
                continue
            ts, text = parsed
            if ts < cutoff:
                continue
            if stats["window_start"] is None or ts < stats["window_start"]:
                stats["window_start"] = ts
            if stats["window_end"] is None or ts > stats["window_end"]:
                stats["window_end"] = ts

            m = RE_TRACE_ID.search(text)
            if m:
                stats["trace_ids"].add(m.group(1))

            m = RE_PLANNER.search(text)
            if m:
                stats["rounds"] += 1
                stats["skills"][m.group(1)] = stats["skills"].get(m.group(1), 0) + 1
                continue  # planner 行不可能是其他模式

            m = RE_TOOL_CALLS.search(text)
            if m:
                stats["tool_call_total"] += 1
                if m.group(1).strip():
                    stats["tool_call_rounds"] += 1
                continue

            m = RE_TOOLS.search(text)
            if m:
                out = m.group(2)
                if RE_COMMAND_FRAME.match(out):
                    stats["command_frames"] += 1
                if RE_ERROR_FRAME.match(out):
                    stats["tool_failures"] += 1
                if RE_DEDUP.search(out):
                    stats["dedup_blocks"] += 1
                continue

            if "[reflector]" in text:
                if "PASS" in text:
                    stats["reflector_pass"] += 1
                if "REVISE" in text:
                    stats["reflector_revise"] += 1
                if RE_BUDGET.search(text):
                    stats["budget_exhausted"] += 1
                continue

            if RE_EMPTY.search(text):
                stats["empty_reply"] += 1
            if RE_SUMMARY_FAIL.search(text):
                stats["summary_failed"] += 1
            if RE_TIMEOUT.search(text):
                stats["timeouts"] += 1
            if RE_AGENT_ERR.search(text):
                stats["agent_errors"] += 1
            if RE_DISPLAY_FAST.search(text):
                stats["display_fastpath"] += 1
    return stats


def fmt_dt(d: dt.datetime | None) -> str:
    return d.strftime("%Y-%m-%d %H:%M") if d else "-"


def render(stats: dict, hours: int) -> str:
    r = stats["rounds"]
    rev = stats["reflector_revise"]
    ps = stats["reflector_pass"]
    tc = stats["tool_call_total"]
    tool_runs = stats["tool_failures"] + stats["command_frames"] + stats["dedup_blocks"]
    lines = [
        f"窗口：最近 {hours}h（{fmt_dt(stats['window_start'])} ~ {fmt_dt(stats['window_end'])}，trace_id 数 {len(stats['trace_ids'])}）",
        "",
        f"对话轮数 rounds                     {r}",
        f"  技能分布                          {stats['skills'] or '-'}",
        f"模型调用轮次（含工具调用）          {tc}（其中调工具 {stats['tool_call_rounds']}）",
        f"工具执行次数（[tools] 行）          {tool_runs}",
        f"  命令帧产出（EFFECT/NAVIGATE/…）   {stats['command_frames']}",
        f"  工具失败（__ERROR__/无效）        {stats['tool_failures']}",
        f"  显示幂等拦截                      {stats['dedup_blocks']}",
        f"reflector PASS                     {ps}",
        f"reflector REVISE                   {rev}   REVISE 率 {rev / max(rev + ps, 1):.1%}",
        f"  反思预算耗尽收尾                 {stats['budget_exhausted']}",
        f"空回复（恢复语兜底）               {stats['empty_reply']}   空回复率 {stats['empty_reply'] / max(r, 1):.2%}",
        f"独立摘要生成失败                   {stats['summary_failed']}",
        f"流式超时（空闲/总时长）            {stats['timeouts']}",
        f"agent 异常（invocation/streaming） {stats['agent_errors']}",
        f"强制显示路由命中                   {stats['display_fastpath']}",
    ]
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--log", default="/tmp/agent_server.log")
    ap.add_argument("--hours", type=int, default=24)
    ap.add_argument("--json", action="store_true", dest="as_json")
    args = ap.parse_args()

    try:
        stats = collect(args.log, args.hours)
    except FileNotFoundError:
        print(f"日志文件不存在：{args.log}", file=sys.stderr)
        sys.exit(2)

    stats["skills"] = dict(sorted(stats["skills"].items(), key=lambda kv: -kv[1]))
    stats["trace_ids"] = sorted(stats["trace_ids"])
    if args.as_json:
        out = {k: v for k, v in stats.items() if k != "trace_ids"}
        out["trace_count"] = len(stats["trace_ids"])
        print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
    else:
        print(render(stats, args.hours))


if __name__ == "__main__":
    main()
