# -*- coding: utf-8 -*-
"""单条 golden 用例独立进程运行器：主脚本 spawn 本脚本跑一条用例，结果写 stdout JSON。
进程隔离：卡死（LLM/HTTP 悬挂）由主脚本按超时 kill，悬挂连接随进程消亡，不污染后续用例。
SIGABRT 注册 faulthandler：主脚本超时先发 SIGABRT 拿全线程栈（卡死点定位），再 kill。
用法: python golden_case_runner.py <case.json 文件路径> [report_dir] [trace_case_suffix]
"""
import faulthandler
import json
import os
import signal
import sys
import time

# SIGABRT 不能 register（Python 自留信号）——enable() 下致命信号（含 SIGABRT）
# 自动 dump 全线程栈到 stderr 后退出，主脚本 communicate 即可拿到卡死点
faulthandler.enable()

# Resolve the repository from this source file, not an installation-specific checkout.
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "eval"))

import run_golden
import golden_trace


def main():
    case = json.load(open(sys.argv[1], encoding="utf-8"))
    # argv[2] 的位置保留（父进程按位置传参，见文件头用法行），但本文件**不写报告**：
    # 子进程的产物只有 stdout 那一行 RESULT，报告由父进程落盘（`_` 前缀 = 刻意不用）。
    _report_dir = sys.argv[2] if len(sys.argv) > 2 else "eval/report/runs"
    run_golden.ensure_agent()
    # 归因用的第 1 轮 gold：多轮用例顶层没有 `gold` 键（双层用例就是这么写的），
    # 取法必须与 `run_golden.main` 一致 —— 走它的具名入口，不在这里自己下标。
    g = run_golden.first_gold(case)
    # Use the shared request, principal and multi-round driver instead of rebuilding them here.
    run_id = golden_trace.resolve_run_id()
    # Keep first-attempt and rerun traces distinct without changing the case ID.
    suffix = sys.argv[3] if len(sys.argv) > 3 else ""
    # Load the assertion corpus before execution; reuse that snapshot for judgment.
    docs = run_golden.judge_corpus()
    t0 = time.time()
    result = run_golden.run_case(case, run_id=run_id, suffix=suffix)
    elapsed = time.time() - t0
    # Missing corpus data leaves dependent assertions unevaluated in both runner modes.
    fails = run_golden.check_case(case, result, docs=docs)
    ok = not fails and not result["error"]
    # Keep full reply text and efficiency fields aligned with the shared runner report.
    requires = bool(g.get("require_tool_calls") or g.get("require_tool_calls_any"))
    out = {
        "id": case["id"],
        "ok": ok,
        "elapsed": round(elapsed, 1),
        "fails": fails,
        "error": result["error"],
        "resets": result["resets"],
        "resets_reasons": result["resets_reasons"],
        # Distinguish archived decision resets from terminal text fallbacks.
        "reset_scopes": result["reset_scopes"],
        "commands": result["commands"],
        "tool_calls": result["tool_calls"],
        "tool_rounds": result["tool_rounds"],
        "requires_tools": requires,
        "text": result["text"],
        "text_truncated": False,
        # Preserve each conversation round in the subprocess result.
        "rounds": [{"round": r["round"], "elapsed": r["elapsed"],
                    "text": r["text"], "frames": run_golden.redact_frames(r.get("frames")),
                    "commands": r["commands"], "tool_calls": r["tool_calls"],
                    "exec_tools": r["exec_tools"], "resets": r["resets"],
                    "reset_scopes": r.get("reset_scopes") or [],
                    "confirm_payloads": r.get("confirm_payloads") or [],
                    "error": r["error"], "trace": r.get("trace")}
                   for r in (result.get("rounds") or [])],
        # Link the exact trace for the attempt.
        "trace": result.get("trace"),
    }
    print("RESULT " + json.dumps(out, ensure_ascii=False))
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
