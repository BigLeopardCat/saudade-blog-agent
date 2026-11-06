# -*- coding: utf-8 -*-
"""全量 golden 进程隔离跑：逐条独立子进程（eval/golden_case_runner.py），
180s 超时 SIGABRT（faulthandler 栈）再 SIGKILL——防 LLM/HTTP 悬挂污染后续用例。
用法（仓库根 cwd）: nohup .venv/bin/python eval/golden_full_run.py
报告: eval/report/runs/<ts>.json **并更新 eval/report/last_run.json**
"""
import argparse
import io
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", line_buffering=True)
# Legacy file entry: resolve modules from this checkout rather than an installed sibling.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import corpus_provenance  # 同目录：语料出处闸（与 run_golden.py 共用同一个判据）
import golden_trace  # 同目录：trace 开关 + 靠 trace 才判得动的 gold 键（见下面的闸）
import report_archive  # 同目录：留档文件名（秒级 ts 同秒撞车 → 见模块头注）

from eval import golden_runtime

golden_runtime.check_environment()
_REPORTS_DIR = golden_runtime.REPORTS_DIR
os.makedirs(_REPORTS_DIR, exist_ok=True)
print(f"[full] runtime={golden_runtime.ENGINE}，留档 {_REPORTS_DIR}/", flush=True)

# Use the selected case file and its adjacent corpus declaration.
_ap = argparse.ArgumentParser(description="全量 golden（逐条独立子进程）")
_ap.add_argument("--golden", default="eval/golden/basic.jsonl",
                 help="换一份用例文件（默认 eval/golden/basic.jsonl）；出处声明要在同目录")
_ARGS = _ap.parse_args()
_PROV_FILE = corpus_provenance.provenance_path_for(_ARGS.golden)
CASES = [json.loads(l) for l in open(_ARGS.golden, encoding="utf-8") if l.strip()]
# Obtain real identities from private environment configuration, matching the shared runner.
_UID_CHANNELS = (("needs_admin_uid", "GOLDEN_ADMIN_UID"),
                 ("needs_user_uid", "GOLDEN_USER_UID"))
# Record skipped IDs before filtering so fixed regressions remain identifiable.
_ALL_TAGS = {c["id"]: (c.get("tags") or []) for c in CASES}
_SKIPPED_IDS = []
for _marker, _env in _UID_CHANNELS:
    _NEED_UID = [c["id"] for c in CASES if c.get(_marker)]
    _real_uid = os.environ.get(_env, "").strip()
    if not _NEED_UID:
        continue
    if not _real_uid:
        CASES = [c for c in CASES if not c.get(_marker)]
        _SKIPPED_IDS += _NEED_UID
        for _cid in _NEED_UID:
            print(f"[skip] {_cid}: SKIP (needs {_env})", flush=True)
    else:
        for _c in CASES:
            if _c.get(_marker):
                _c.setdefault("context", {})["user_id"] = int(_real_uid)
# Require explicit real-write opt-in and ready fixtures, using the shared eligibility rules.
_REAL_WRITE_ENV = "GOLDEN_ALLOW_REAL_WRITE"
_NEED_WRITE = [c["id"] for c in CASES if c.get("needs_real_write")]
# 「按设计不跑」的那批单列（口径同 run_golden.py）：真写用例默认不跑，这不是分母缺失。
_WRITE_SKIPPED: list[str] = []
if _NEED_WRITE and not os.environ.get(_REAL_WRITE_ENV, "").strip():
    CASES = [c for c in CASES if not c.get("needs_real_write")]
    _SKIPPED_IDS += _NEED_WRITE
    _WRITE_SKIPPED = list(_NEED_WRITE)
    for _cid in _NEED_WRITE:
        print(f"[skip] {_cid}: SKIP (needs {_REAL_WRITE_ENV}=1 —— 真写用例默认不自动跑)", flush=True)
if any(c.get("requires_fixture") for c in CASES):
    # 局部导入：本判据只在有夹具用例时才需要（它会拉起 tools.base，父进程平时不必付这笔钱）。
    # `eval/` 在 sys.path 上（见文件头第一段）。
    import golden_fixture  # noqa: E402
    # 闸的实现只有一处（`golden_fixture.gate`，两族夹具都在里面）——与 run_golden.py
    # **共用同一个函数**，避免两份实现与注释口径漂移。
    CASES, _DROPPED, _LINES = golden_fixture.gate(CASES)
    _SKIPPED_IDS += _DROPPED
    for _ln in _LINES:
        print(_ln, flush=True)
# Trace-dependent expectations remain unevaluated when their evidence is unavailable.
_TRACE_SKIPPED: list[str] = []
if not golden_trace.enabled():
    _TRACE_SKIPPED = [c["id"] for c in CASES if golden_trace.trace_derived_keys(c)]
    if _TRACE_SKIPPED:
        _drop = set(_TRACE_SKIPPED)
        CASES = [c for c in CASES if c["id"] not in _drop]
        _SKIPPED_IDS += _TRACE_SKIPPED
        for _cid in _TRACE_SKIPPED:
            print(f"[skip] {_cid}: SKIP (判据要读 planner.ledger_frame trace 事件，"
                  f"而 {golden_trace.ENV_OFF} 关着 trace —— 未评估，不是模型退化)",
                  flush=True)
# Check the selected corpus with the shared provenance implementation.
_CORPUS_BAD = False
_CORPUS_SNAP = corpus_provenance.snapshot_docs()
_CORPUS_STATE, _CORPUS_DETAIL, _CORPUS_ROW = corpus_provenance.check_corpus_premises(
    _CORPUS_SNAP, _PROV_FILE)
if _CORPUS_STATE == corpus_provenance.CORPUS_PROV_FOREIGN:
    _CORPUS_BAD = True
    _SKIPPED_IDS += [c["id"] for c in CASES]
    _n_corpus = len(CASES)          # 先记下来：下面 `CASES` 就被清空了
    CASES = []
for _line in corpus_provenance.report_lines(_CORPUS_STATE, _CORPUS_DETAIL, _CORPUS_ROW):
    print(_line, flush=True)
if _CORPUS_BAD:
    print(f"[corpus-premise] ⇒ {_n_corpus} 条用例**全部未评估**（未评估 ≠ 通过；退出码 3）",
          flush=True)
# Check that the declared source entities remain present before running their cases.
_ENTITY_BAD = False
CASES, _ENTITY_SKIPPED, _ENTITY_ROWS = corpus_provenance.check_entity_premises(
    CASES, _CORPUS_SNAP)
_SKIPPED_IDS += _ENTITY_SKIPPED
_ENTITY_BAD = bool(_ENTITY_SKIPPED)
for _line in corpus_provenance.entity_report_lines(_ENTITY_ROWS, _ENTITY_SKIPPED):
    print(_line, flush=True)
# An empty evaluated population cannot produce a passing aggregate.
if not CASES:
    _code = 3 if (_TRACE_SKIPPED or _CORPUS_BAD or _ENTITY_BAD) else 2
    print("[full] ⚠ 一条用例都没剩下（被身份闸 / 真写闸 / 夹具闸 / trace 闸 / 实体闸 / "
          "语料出处闸"
          f"摘干净了）—— 这一轮**没有评测任何东西**：空分母不是一个通过率，退出码 {_code}"
          "（不是 0）"
          + ("；其中**语料不是这套 golden 的那一份**是主因（[corpus-premise] 那几行有"
             "锚点对账）" if _CORPUS_BAD else "")
          + ("；其中 trace 关着是主因（那几条用例的判据要读 trace 事件）"
             if _TRACE_SKIPPED and not _CORPUS_BAD else ""), flush=True)
    sys.exit(_code)
RUNNER = "eval/golden_case_runner.py"
TMPDIR = "/tmp/golden_cases"
TIMEOUT = 180
os.makedirs(TMPDIR, exist_ok=True)

# Resolve one run ID in the parent and share it with child processes.
_TRACE_ON = golden_trace.enabled()
GOLDEN_RUN = golden_trace.resolve_run_id() if _TRACE_ON else None
if GOLDEN_RUN:
    os.environ[golden_trace.ENV_RUN] = GOLDEN_RUN

def spawn_case(case: dict, suffix: str = "") -> dict:
    """Run one case in an isolated subprocess.

    A distinct suffix keeps first-attempt and rerun traces separate. Multi-round
    conversation and approval behavior is owned by run_golden.run_case."""
    cid = case["id"]
    json.dump(case, open(f"{TMPDIR}/{cid}.json", "w", encoding="utf-8"), ensure_ascii=False)
    # Use the current interpreter so isolated runs retain the selected environment.
    argv = [sys.executable, RUNNER, f"{TMPDIR}/{cid}.json", _REPORTS_DIR]
    if suffix:
        argv.append(suffix)
    t0 = time.time()
    proc = subprocess.Popen(
        argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8",
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
        r["_timeout"] = True
    return r


results, failed, timed_out = [], 0, []
t_all = time.time()
for i, case in enumerate(CASES, 1):
    cid = case["id"]
    r = spawn_case(case)
    if r.pop("_timeout", False):
        timed_out.append(cid)
    # Retain case tags for regression grouping and report attribution.
    r["tags"] = case.get("tags") or []
    results.append(r)
    ok = "PASS" if r["ok"] else "FAIL"
    fails = "; ".join(r.get("fails") or [])[:100]
    print(f"[{i}/{len(CASES)}] {ok} {cid:35s} {r.get('elapsed', 0):6.1f}s resets={r.get('resets', 0)} {fails}", flush=True)

# The historical runner records first-attempt failures before any regression rerun.
_CASE_BY_ID = {c["id"]: c for c in CASES}
_RERUN = [r["id"] for r in results if not r["ok"] and "regression" in r["tags"]]
if _RERUN:
    print(f"\n[rerun] 回归组首跑红 {len(_RERUN)} 条，各重跑一次再判：{_RERUN}", flush=True)
for r in results:
    case = _CASE_BY_ID.get(r["id"], {})
    if r["id"] not in _RERUN:
        r["rerun"] = None
        r["final_ok"] = r["ok"]
        continue
    rr = spawn_case(case, "_rerun")
    rr.pop("_timeout", None)
    r["rerun"] = rr
    r["final_ok"] = r["ok"] or rr["ok"]
    print(f"[rerun] {r['id']}: 首跑红 → "
          + ("复跑绿（按方差放行，首跑红仍在报告里）" if rr["ok"] else "复跑仍红（真 FAIL）"),
          flush=True)
    if rr["ok"]:
        print(f"          └ 首跑失败项：{'; '.join(r.get('fails') or [])[:150]}", flush=True)
        print(f"          └ 首跑 trace: {r.get('trace')}", flush=True)
        print(f"          └ 复跑 trace: {rr.get('trace')}", flush=True)

failed_first = sum(1 for r in results if not r["ok"])
failed = sum(1 for r in results if not r["final_ok"])
_REG = [r for r in results if "regression" in r["tags"]]
_reg_bad = [r["id"] for r in _REG if not r["final_ok"]]
_reg_flaked = [r["id"] for r in _REG if not r["ok"] and r["final_ok"]]
if failed != failed_first:
    print(f"[rerun] 终判 {len(CASES) - failed}/{len(CASES)}（首跑红 {failed_first} 条，"
          f"其中 {failed_first - failed} 条复跑绿）", flush=True)

dur = time.time() - t_all
print(f"\n=== 汇总：{len(CASES) - failed}/{len(CASES)} 通过（超时 {len(timed_out)}"
      + (f"，首跑红 {failed_first} 条其中 {failed_first - failed} 条复跑绿"
         if failed != failed_first else "") + "）===")
print(f"总耗时 {dur:.0f}s 基线 min={min(r['elapsed'] for r in results):.1f}s "
      f"P50={sorted(r['elapsed'] for r in results)[len(results)//2]:.1f}s "
      f"P95={sorted(r['elapsed'] for r in results)[int(len(results)*0.95)-1]:.1f}s "
      f"max={max(r['elapsed'] for r in results):.1f}s")
if timed_out:
    print("超时用例:", timed_out)
# Report fixed regressions separately from capability samples.
print(f"回归组: {len(_REG) - len(_reg_bad)}/{len(_REG)}"
      + (f"  ⚠ 红：{_reg_bad}" if _reg_bad else "")
      + (f"  ⚠ 复跑才绿：{_reg_flaked}（首跑红，已按方差放行——首跑/复跑两条都在报告里）"
         if _reg_flaked else ""))

# Share Wilson intervals and tag grouping with the in-process runner.
from run_golden import by_tag_stats, wilson_ci  # noqa: E402

_TAGS_MAP = {r["id"]: r["tags"] for r in results}
# `ts`（= 留档文件名那串戳）在**写的那一刻**由 report_archive 给出（见文件末尾）——
# 这里先留空位，写之前补上：名字与报告里那一格因此恒成对，中间也无需预告一个可能
# 被顺延的戳。
report = {"ts": "", "corpus": "full", "total": len(CASES), "passed": len(CASES) - failed,
          # Retain the actual corpus provenance findings.
          "corpus_provenance": _CORPUS_ROW,
          "engine": golden_runtime.ENGINE,
          "failed": failed, "latency_s": [r["elapsed"] for r in results],
          # Report pass-rate estimates together with their denominators.
          "pass_rate": round((len(CASES) - failed) / len(CASES), 4) if CASES else 0.0,
          "pass_rate_ci95": wilson_ci(len(CASES) - failed, len(CASES)),
          "by_tag": by_tag_stats(results, _TAGS_MAP),
          # 这个跑法就是全量的定义（逐条独立子进程），恒 True——留着是为了让两个
          # 报告文件的字段表形状一致（读报告的地方不必先问"这是哪个跑法写的"）。
          "full_run": True,
          "skipped_ids": list(_SKIPPED_IDS),
          # 其中「按设计不跑」的那批单列（口径同 run_golden.py）：真写用例默认不跑。
          "skipped_real_write_ids": list(_WRITE_SKIPPED),
          # 其中「trace 关着 ⇒ 判据没有证据链」的那批单列（口径同 run_golden.py 的
          # `skipped_trace_ids`）：它们是**未评估**，不是"过"，也不是模型退化。
          "skipped_trace_ids": list(_TRACE_SKIPPED),
          # 其中「判据点名的实体已不在公开面上」的那批单列（口径同 run_golden.py 的
          # `skipped_entity_ids`）：同源纪律——**未评估要单列**。
          "skipped_entity_ids": list(_ENTITY_SKIPPED),
          "entity_checks": _ENTITY_ROWS,
          # Preserve the first-attempt failure count independently of the terminal count.
          "failed_first_run": failed_first,
          # Retain the regression block used to identify traces that must be preserved.
          "regression": {"total": len(_REG),
                         "passed": len(_REG) - len(_reg_bad),
                         "failed_ids": _reg_bad,
                         "all_passed": not _reg_bad,
                         "flaked_ids": _reg_flaked,
                         # 跳过的用例里只有回归组值得单列（口径同 run_golden.py：跳过会改变
                         # 通过率分母，回归组的硬判 100% 更要看得见谁没跑）
                         "skipped_ids": [s for s in _SKIPPED_IDS
                                         if "regression" in _ALL_TAGS.get(s, [])]},
          # Record the shared evaluation trace run directory.
          "trace_run": GOLDEN_RUN,
          "cases": results}
# 留档名在**写的那一刻**取（`O_EXCL` 占位，见 eval/report_archive.py 头注）：秒级 ts 会让
# 同一秒的两次跑（本跑法 + 一次 `--only` 调试跑）同名互相覆盖。`ts` 跟着文件名走（报告
# 里那一格与文件名成对，读的人不用换算）。
with report_archive.open_archive(_REPORTS_DIR) as (archive, f):
    ts = os.path.splitext(os.path.basename(archive))[0]
    report["ts"] = ts
    json.dump(report, f, ensure_ascii=False, indent=1)
with open("eval/report/last_run.json", "w", encoding="utf-8") as f:
    json.dump(report, f, ensure_ascii=False, indent=1)
print(f"报告: {archive}（并更新 eval/report/last_run.json）")
_lo, _hi = report["pass_rate_ci95"]
print(f"通过率: {report['pass_rate']:.3f}（Wilson 95% 区间 {_lo:.3f}–{_hi:.3f}）")
_weak = [(t, b) for t, b in report["by_tag"].items()
         if b["total"] >= 3 and b["failed_ids"]]
if _weak:
    print("弱项 tag: " + "；".join(
        f"{t} {b['passed']}/{b['total']}（区间 {b['ci95'][0]:.2f}–{b['ci95'][1]:.2f}）"
        f" 红={b['failed_ids']}" for t, b in _weak))
if GOLDEN_RUN:
    _ntr = sum(1 for r in results if r.get("trace"))
    print(f"trace: logs/agent/golden_traces/{GOLDEN_RUN}/（{_ntr}/{len(results)} 条落盘）")
    _pruned = golden_trace.prune()
    if _pruned:
        print(f"trace 清理: 删掉 {len(_pruned)} 个旧目录（{_pruned[0]} … {_pruned[-1]}）")
