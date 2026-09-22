# -*- coding: utf-8 -*-
"""全量 golden 进程隔离跑：逐条独立子进程（eval/golden_case_runner.py），
180s 超时 SIGABRT（faulthandler 栈）再 SIGKILL——防 LLM/HTTP 悬挂污染后续用例。
用法（仓库根 cwd）: nohup .venv/bin/python eval/golden_full_run.py
报告: eval/report/runs/<ts>.json（与 run_golden.py 同目录双写 last_run.json 不冲突）
"""
import io, json, os, signal, subprocess, sys, time
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", line_buffering=True)
sys.path.insert(0, "eval")

CASES = [json.loads(l) for l in open("eval/golden/basic.jsonl", encoding="utf-8") if l.strip()]
# 管理助手「读后台」用例需要真实 admin uid（20260921）：口径与 run_golden.py 一致
# ——未设 GOLDEN_ADMIN_UID 就明确跳过并打印（如实计入分母变化，不静默豁免）。
# 子进程继承环境变量，但**用例文件是父进程写的**，所以 uid 注入必须在这里做。
_ADMIN_UID = os.environ.get("GOLDEN_ADMIN_UID", "").strip()
_NEED_UID = [c["id"] for c in CASES if c.get("needs_admin_uid")]
if _NEED_UID and not _ADMIN_UID:
    CASES = [c for c in CASES if not c.get("needs_admin_uid")]
    for _cid in _NEED_UID:
        print(f"[skip] {_cid}: SKIP (needs GOLDEN_ADMIN_UID)", flush=True)
elif _ADMIN_UID:
    for _c in CASES:
        if _c.get("needs_admin_uid"):
            _c.setdefault("context", {})["user_id"] = int(_ADMIN_UID)
RUNNER = "eval/golden_case_runner.py"
TMPDIR = "/tmp/golden_cases"
TIMEOUT = 180
os.makedirs(TMPDIR, exist_ok=True)

# golden trace（20260922）：run_id 在**父进程**定一次，子进程经环境变量继承 ⇒ 整个 run 落
# 同一个目录（子进程各自 resolve 就会把一次全量散成上百个目录）。GOLDEN_NO_TRACE=1 整体关。
import golden_trace
_TRACE_ON = golden_trace.enabled()
GOLDEN_RUN = golden_trace.resolve_run_id() if _TRACE_ON else None
if GOLDEN_RUN:
    os.environ[golden_trace.ENV_RUN] = GOLDEN_RUN

results, failed, timed_out = [], 0, []
t_all = time.time()
for i, case in enumerate(CASES, 1):
    cid = case["id"]
    json.dump(case, open(f"{TMPDIR}/{cid}.json", "w", encoding="utf-8"), ensure_ascii=False)
    t0 = time.time()
    proc = subprocess.Popen(
        [".venv/bin/python", RUNNER, f"{TMPDIR}/{cid}.json", "eval/report/runs"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8",
    )
    try:
        out, err = proc.communicate(timeout=TIMEOUT)
        elapsed = time.time() - t0
        tail = out.strip().splitlines()[-1] if out.strip() else ""
        if tail.startswith("RESULT "):
            r = json.loads(tail[7:])
        else:
            r = {"id": cid, "ok": False, "elapsed": round(elapsed, 1),
                 "fails": [f"runner 无结果: {(err or out)[-200:]}"],
                 "error": (err or out)[-200:], "resets": 0, "resets_reasons": []}
    except subprocess.TimeoutExpired:
        elapsed = time.time() - t0
        proc.send_signal(signal.SIGABRT)
        try:
            _, err = proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            _, err = proc.communicate()
        r = {"id": cid, "ok": False, "elapsed": round(elapsed, 1),
             "fails": ["超时 180s" + (f"（stderr: {err[-200:]}）" if err else "")],
             "error": (err or "")[-300:], "resets": 0, "resets_reasons": []}
        timed_out.append(cid)
    results.append(r)
    ok = "PASS" if r["ok"] else "FAIL"
    fails = "; ".join(r.get("fails") or [])[:100]
    print(f"[{i}/{len(CASES)}] {ok} {cid:35s} {r.get('elapsed', 0):6.1f}s resets={r.get('resets', 0)} {fails}", flush=True)
    if not r["ok"]:
        failed += 1

dur = time.time() - t_all
print(f"\n=== 汇总：{len(CASES) - failed}/{len(CASES)} 通过（超时 {len(timed_out)}）===")
print(f"总耗时 {dur:.0f}s 基线 min={min(r['elapsed'] for r in results):.1f}s "
      f"P50={sorted(r['elapsed'] for r in results)[len(results)//2]:.1f}s "
      f"P95={sorted(r['elapsed'] for r in results)[int(len(results)*0.95)-1]:.1f}s "
      f"max={max(r['elapsed'] for r in results):.1f}s")
if timed_out:
    print("超时用例:", timed_out)

ts = time.strftime("%Y%m%d_%H%M%S")
report = {"ts": ts, "corpus": "full", "total": len(CASES), "passed": len(CASES) - failed,
          "failed": failed, "latency_s": [r["elapsed"] for r in results],
          # 这一轮的 trace 目录（20260922）：run_id 是本进程定的，子进程都落在它下面
          "trace_run": GOLDEN_RUN,
          "cases": results}
with open(f"eval/report/runs/{ts}.json", "w", encoding="utf-8") as f:
    json.dump(report, f, ensure_ascii=False, indent=1)
print(f"报告: eval/report/runs/{ts}.json")
if GOLDEN_RUN:
    _ntr = sum(1 for r in results if r.get("trace"))
    print(f"trace: logs/agent/golden_traces/{GOLDEN_RUN}/（{_ntr}/{len(results)} 条落盘）")
    _pruned = golden_trace.prune()
    if _pruned:
        print(f"trace 清理: 删掉 {len(_pruned)} 个旧目录（{_pruned[0]} … {_pruned[-1]}）")
