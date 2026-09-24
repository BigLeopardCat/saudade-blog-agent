# -*- coding: utf-8 -*-
"""单条 golden 用例独立进程运行器：主脚本 spawn 本脚本跑一条用例，结果写 stdout JSON。
进程隔离：卡死（LLM/HTTP 悬挂）由主脚本按超时 kill，悬挂连接随进程消亡，不污染后续用例。
SIGABRT 注册 faulthandler：主脚本超时先发 SIGABRT 拿全线程栈（卡死点定位），再 kill。
用法: python golden_case_runner.py <case.json 文件路径> [report_dir] [trace_case_suffix]
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
    # 请求构造只走 run_golden.build_request（20260920 前本文件手写了一份、漏了 executions）；
    # 调用者身份同理只走 run_golden.build_principal（20260921 管理助手用例需要 role，
    # 隔离跑法若各自维护一份，就会重演"两个跑法结论不同"那次）——20260925 起连**轮次
    # 驱动**（第 2 轮要令牌、会话 id 必须一致）也只走 run_golden.run_case：本文件不再
    # 自己发请求，它只负责"一条用例一个进程"这层壳。
    # golden trace（20260922）：run_id 由父进程经 GOLDEN_TRACE_RUN 传进来（隔离跑法下
    # 一个 run 一个目录，子进程各自 resolve 会散成 N 个目录）。父进程没设时退回当前
    # 时刻——单条手跑也照样有 trace，只是那条独占一个目录。
    run_id = golden_trace.resolve_run_id()
    # 复跑（20260924）：父进程给的名字后缀 —— 回归组首跑红要重跑一次，两次必须落
    # **两份** trace（同名文件会把首跑那份覆盖掉，而"首跑为什么红"正是复跑要回答的）。
    # 结果 dict 的 id 不带后缀（父进程按 id 归并）。
    suffix = sys.argv[3] if len(sys.argv) > 3 else ""
    t0 = time.time()
    result = run_golden.run_case(case, run_id=run_id, suffix=suffix)
    elapsed = time.time() - t0
    # 语料快照同进程内取一次（20260925）：`require_doc_terms` 要拿**正文**派生术语，
    # 取不到就得判「未评估」而不是静默通过——两个跑法（进程内 / 隔离）在这一点上必须
    # 给出同一个结论（判据侧不做"这个跑法没接上就放行"的妥协）。
    fails = run_golden.check_case(case, result, docs=run_golden.judge_corpus())
    ok = not fails and not result["error"]
    # 字段表与 run_golden.py 的 `results.append({...})` **逐字段对齐**（20260924）：两个
    # 跑法（进程内 / 隔离子进程）写出形状不同的报告，"红了照报告读现场"这条纪律就只在
    # 一个跑法里成立。此前这里差三处：
    #   ① `text` 被 `[:300]` 截断——报告是隔离跑法唯一的产物（stdout 只有一行 JSON），
    #      截断等于**把现场丢掉**：红条要看的正是"它到底说了什么"，而前 300 字常常只是
    #      客套。现在全文落报告，并显式写 `text_truncated: false` 让读的人不必去猜阈值。
    #   ② `tool_rounds`（效率基线的规划轮数）缺席 ⇒ 隔离跑法算不出这条指标。
    #   ③ `requires_tools`（工具类/非工具类归因）缺席 ⇒ 同上。判据与 run_golden 一致：
    #      该用例**要求**过工具调用即算工具类。
    # `tags` 仍不在这里出——父进程（golden_full_run.py）从用例文件本地补，两边不重复。
    requires = bool(g.get("require_tool_calls") or g.get("require_tool_calls_any"))
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
        "tool_rounds": result["tool_rounds"],
        "requires_tools": requires,
        "text": result["text"],
        "text_truncated": False,
        # 逐轮留档（20260925）：与 run_golden.py 的报告同名字段同形状——多轮用例的
        # "第 1 轮弹没弹卡"只在第 1 轮里，末轮的扁平结果看不见它。
        # `confirm_payloads`（解开的载荷，判据 `require_confirm_payload` 要读它）**必须
        # 与进程内跑法一样在这里出**：隔离跑法的报告就是本文件这行 stdout JSON，少一个
        # 字段，同一条用例在两个跑法下结论就会不同（`[]` vs 有值 ⇒ 一边 PASS 一边 FAIL）。
        # 原始令牌两处都不落盘——它是 10 分钟有效的写授权凭据。
        "rounds": [{"round": r["round"], "elapsed": r["elapsed"],
                    "text": r["text"], "frames": run_golden.redact_frames(r.get("frames")),
                    "commands": r["commands"], "tool_calls": r["tool_calls"],
                    "exec_tools": r["exec_tools"], "resets": r["resets"],
                    "confirm_payloads": r.get("confirm_payloads") or [],
                    "error": r["error"], "trace": r.get("trace")}
                   for r in (result.get("rounds") or [])],
        # 这一条的 trace 路径（20260922）：父进程报告里带出去，红条能直接指着读
        "trace": result.get("trace"),
    }
    print("RESULT " + json.dumps(out, ensure_ascii=False))
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
