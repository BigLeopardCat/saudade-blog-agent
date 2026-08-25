# -*- coding: utf-8 -*-
"""L2 golden set 最小版运行器：真实 agent 端到端（真实 LLM + 真实工具）。

每条 golden 样本断言"行为"而非"实现"：
  - 动作通道：命令帧（EFFECT:/NAVIGATE:/AUTO_NAVIGATE:/DARKMODE:）是否如期望产生/禁止
  - 声称通道：最终正文是否命中六道防幻觉闸门（声称检测，动作已执行时豁免——诚实声称）
  - 文本关键词 / 非空 / 兜底句泄漏
并统计六个 gate 在真实对话里的触发率（为"评测驱动删码"提供数据）。

用法（cd saudade-blog-agent）：
  .venv/bin/python eval/run_golden.py               # 全量
  .venv/bin/python eval/run_golden.py --limit 3     # 前 3 条（调试）
  .venv/bin/python eval/run_golden.py --only nav_friends_down
退出码：0=全过 1=有失败（CI 可接）
"""
import argparse
import asyncio
import json
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

# 复用 server 内部链路（不走 HTTP，与 test_fallback_replay.py 同模式）
import server
from server import ChatRequest, _build_messages, _run_agent_stream_to_queue
from agent import create_agent
from langchain_core.messages import AIMessage, AIMessageChunk, ToolMessage

# 六道程序化防幻觉闸门——复用为 golden 的"声称检测器"
from agent.graph import (
    _fake_command_in,
    _fake_claim_in,
    _fake_promise_in,
    _fake_toolclaim_in,
    _fake_effectclaim_in,
    _fake_effectpromise_in,
)

GATE_NAMES = ["command", "claim", "promise", "toolclaim", "effectclaim", "effectpromise"]
GATE_FUNCS = [
    _fake_command_in,
    _fake_claim_in,
    _fake_promise_in,
    _fake_toolclaim_in,
    _fake_effectclaim_in,
    _fake_effectpromise_in,
]
CMD_PREFIXES = ("EFFECT:", "NAVIGATE:", "AUTO_NAVIGATE:", "DARKMODE:")
GOLDEN_FILE = "eval/golden/basic.jsonl"
REPORT_FILE = "eval/report/last_run.json"


def ensure_agent() -> None:
    if server._agent is None:
        t0 = time.time()
        server._agent = create_agent()
        print(f"[init] 编译图构建完成：{time.time() - t0:.1f}s")


def run_one(req: ChatRequest) -> dict:
    """跑一轮真实对话（内部链路），从帧流提取最终文本 / 命令帧 / 事件。"""
    loop = asyncio.new_event_loop()
    queue = asyncio.Queue()
    frames = []

    def drain():
        while True:
            item = loop.run_until_complete(queue.get())
            if item is None:
                break
            frames.append(item)

    t = threading.Thread(target=drain)
    t.start()
    with ThreadPoolExecutor(max_workers=1) as ex:
        ex.submit(
            _run_agent_stream_to_queue,
            _build_messages(req), "golden_thread", queue, loop, req.user_id,
        ).result()
    t.join()
    loop.close()

    final_text = ""
    commands: list[str] = []
    resets = 0
    error = None
    for item in frames:
        if isinstance(item, str) and item.startswith("__RESET__"):
            final_text = ""  # REVISE/兜底轮作废 → 清空（与前端最终显示一致）
            resets += 1
        elif isinstance(item, AIMessageChunk) and item.content:
            final_text += str(item.content)
        elif isinstance(item, ToolMessage) and item.content:
            for line in str(item.content).splitlines():
                s = line.strip()
                if s.startswith(CMD_PREFIXES):
                    commands.append(s)
        elif isinstance(item, BaseException):
            error = str(item)
    return {"text": final_text, "commands": commands, "resets": resets, "error": error}


def check_gold(gold: dict, result: dict) -> list[str]:
    """逐项断言 golden 期望，返回失败原因列表（空 = 通过）。"""
    text = result["text"]
    commands = result["commands"]
    fails: list[str] = []

    if gold.get("nonempty", True) and not text.strip():
        fails.append("回复为空")

    for pre in gold.get("require_cmd_prefixes", []):
        hits = [c for c in commands if c.startswith(pre)]
        if not hits:
            fails.append(f"缺少 {pre} 命令帧")
        elif gold.get("require_cmd_contains") and not any(
            gold["require_cmd_contains"] in c for c in hits
        ):
            fails.append(f"{pre} 命令内容不符（期望含 {gold['require_cmd_contains']}）")

    for pre in gold.get("forbid_cmd_prefixes", []):
        if any(c.startswith(pre) for c in commands):
            fails.append(f"不应产生 {pre} 命令帧")

    for kw in gold.get("text_contains", []):
        if kw not in text:
            fails.append(f"文本缺少关键词 {kw!r}")
    for kw in gold.get("text_not_contains", []):
        if kw in text:
            fails.append(f"文本不应包含 {kw!r}")

    # 声称通道检测：本轮有命令帧（动作已执行）→ 声称诚实，豁免；无命令 → 声称即幻觉
    if gold.get("no_claim_gates", False):
        hit = [n for n, f in zip(GATE_NAMES, GATE_FUNCS) if f([AIMessage(content=text)])]
        if hit:
            fails.append(f"声称类检测命中: {hit}")

    if server._QA_FALLBACK_SENTENCE in text:
        fails.append("触发了诚实兜底句（本轮行为失败）")

    return fails


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="只跑前 N 条（调试）")
    ap.add_argument("--only", default="", help="只跑指定 id")
    args = ap.parse_args()

    ensure_agent()

    cases = [json.loads(line) for line in open(GOLDEN_FILE, encoding="utf-8") if line.strip()]
    if args.only:
        cases = [c for c in cases if c["id"] == args.only]
    if args.limit:
        cases = cases[: args.limit]
    print(f"[run] {len(cases)} 条 golden 样本（真实 LLM，约 {len(cases) * 30}s）\n")

    gate_hits = {n: 0 for n in GATE_NAMES}
    gate_cases = {n: [] for n in GATE_NAMES}
    results = []
    failed = 0

    for i, case in enumerate(cases, 1):
        g = case["gold"]
        ctx = case.get("context", {})
        req = ChatRequest(
            message=case["user_input"],
            current_url=ctx.get("current_url", "/"),
            page_title=ctx.get("page_title", ""),
            user_id=ctx.get("user_id", 0),
            needs_summary=g.get("needs_summary", False),
            current_effects=ctx.get("current_effects", ""),
            current_darkmode=ctx.get("current_darkmode", ""),
        )
        t0 = time.time()
        result = run_one(req)
        elapsed = time.time() - t0
        fails = check_gold(g, result)
        ok = not fails and not result["error"]

        # gate 触发统计（对最终正文，与声称检测同口径）
        for n, f in zip(GATE_NAMES, GATE_FUNCS):
            if f([AIMessage(content=result["text"])]):
                gate_hits[n] += 1
                gate_cases[n].append(case["id"])

        status = "PASS" if ok else "FAIL"
        if not ok:
            failed += 1
        tail = result["text"].replace("\n", " ")[:60]
        print(f"[{i:>2}/{len(cases)}] {status} {case['id']:<22} {elapsed:>5.1f}s  {tail}")
        if not ok:
            err = result.get("error") or ""
            print(f"          └ {fails or f'error: {err}'}")
        results.append({
            "id": case["id"], "tags": case.get("tags", []), "ok": ok,
            "elapsed": round(elapsed, 1),
            "fails": fails, "error": result["error"],
            "commands": result["commands"], "resets": result["resets"],
            "text": result["text"],
        })

    # 报告
    import os
    os.makedirs("eval/report", exist_ok=True)
    report = {
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        "total": len(cases), "passed": len(cases) - failed, "failed": failed,
        "gate_hits": gate_hits, "gate_cases": gate_cases,
        "cases": results,
    }
    with open(REPORT_FILE, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=1)

    print(f"\n=== 汇总：{len(cases) - failed}/{len(cases)} 通过 ===")
    for n in GATE_NAMES:
        print(f"  gate[{n}] 命中 {gate_hits[n]}/{len(cases)} 条: {gate_cases[n]}")
    print(f"报告: {REPORT_FILE}")
    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
