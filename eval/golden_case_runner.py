# -*- coding: utf-8 -*-
"""单条 golden 用例独立进程运行器：主脚本 spawn 本脚本跑一条用例，结果写 stdout JSON。
进程隔离：卡死（LLM/HTTP 悬挂）由主脚本按超时 kill，悬挂连接随进程消亡，不污染后续用例。
SIGABRT 注册 faulthandler：主脚本超时先发 SIGABRT 拿全线程栈（卡死点定位），再 kill。
用法: python golden_case_runner.py <case.json 文件路径> [report_dir]
"""
import faulthandler
import json
import signal
import sys
import time

# SIGABRT 不能 register（Python 自留信号）——enable() 下致命信号（含 SIGABRT）
# 自动 dump 全线程栈到 stderr 后退出，主脚本 communicate 即可拿到卡死点
faulthandler.enable()

sys.path.insert(0, "/home/ubuntu/memory_blog_rust/saudade-blog-agent")
sys.path.insert(0, "/home/ubuntu/memory_blog_rust/saudade-blog-agent/eval")

import run_golden


def main():
    case = json.load(open(sys.argv[1], encoding="utf-8"))
    report_dir = sys.argv[2] if len(sys.argv) > 2 else "eval/report/runs"
    run_golden.ensure_agent()
    g = case["gold"]
    # 请求构造只走 run_golden.build_request（20260920 前本文件手写了一份、漏了 executions）；
    # 调用者身份同理只走 run_golden.build_principal（20260921 管理助手用例需要 role，
    # 隔离跑法若各自维护一份，就会重演"两个跑法结论不同"那次）
    req = run_golden.build_request(case)
    t0 = time.time()
    result = run_golden.run_one(req, run_golden.build_principal(case))
    elapsed = time.time() - t0
    fails = run_golden.check_gold(g, result)
    ok = not fails and not result["error"]
    out = {
        "id": case["id"],
        "ok": ok,
        "elapsed": round(elapsed, 1),
        "fails": fails,
        "error": result["error"],
        "resets": result["resets"],
        "resets_reasons": result["resets_reasons"],
        "commands": result["commands"],
        "tool_calls": result["tool_calls"],
        "text": result["text"][:300],
    }
    print("RESULT " + json.dumps(out, ensure_ascii=False))
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
