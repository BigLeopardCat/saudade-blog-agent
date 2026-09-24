# -*- coding: utf-8 -*-
"""全量 golden 进程隔离跑：逐条独立子进程（eval/golden_case_runner.py），
180s 超时 SIGABRT（faulthandler 栈）再 SIGKILL——防 LLM/HTTP 悬挂污染后续用例。
用法（仓库根 cwd）: nohup .venv/bin/python eval/golden_full_run.py
报告: eval/report/runs/<ts>.json **并更新 eval/report/last_run.json**
（20260924 起 last_run.json 的语义 = 最近一次**全量**跑；run_golden.py 那边同样只在
 full_run 时写它，两边不打架——此前注释写着"双写不冲突"，实际是 run_golden 每次调试跑
 都会覆盖它，最后一次 `--only <单条>` 就把它写成了 total=1）

20260924：回归组（tags 含 regression）首跑红 → **各重跑一次再判**（口径与
run_golden.py 逐字一致，复跑也走独立子进程）：复跑仍红=真 FAIL，复跑绿=按方差放行但
必须响——首跑红与复跑绿两条都进报告（cases[].rerun / regression.flaked_ids /
failed_first_run）与汇总打印。复跑的 trace 用 `<case>__rerun` 名，首跑那份不被覆盖。
"""
import io, json, os, signal, subprocess, sys, time
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", line_buffering=True)
sys.path.insert(0, "eval")

CASES = [json.loads(l) for l in open("eval/golden/basic.jsonl", encoding="utf-8") if l.strip()]
# 需要**真实身份**的用例（管理员读后台 20260921；普通用户被拒那条 20260924）：口径与
# run_golden.py 逐字一致——两条通道（GOLDEN_ADMIN_UID / GOLDEN_USER_UID），未设就明确
# 跳过并打印（如实计入分母变化，不静默豁免）。
# 子进程继承环境变量，但**用例文件是父进程写的**，所以 uid 注入必须在这里做。
_UID_CHANNELS = (("needs_admin_uid", "GOLDEN_ADMIN_UID"),
                 ("needs_user_uid", "GOLDEN_USER_UID"))
# 跳过的用例在**过滤之前**记下来（20260924 修）：报告里的 skipped_ids 要按"是不是回归组"
# 分拣，而回归组的分拣靠 tags —— 过滤之后再查就查不到了（用例已不在 CASES 里）。
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
# 真写用例的两道闸（20260925）：**口径与 run_golden.py 逐字一致**（那两段是判据，
# 不是跑法的实现细节——两个跑法各写一套就会重演"同一个用例两个结论"）。
# 顺序也一样：先问"谁有权触发真写"（默认没有许可），再看夹具在不在位。
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
_NEED_FIX = {c["id"]: str(c["requires_fixture"]) for c in CASES if c.get("requires_fixture")}
if _NEED_FIX:
    # 局部导入：本判据只在有夹具用例时才需要（它会拉起 tools.base，父进程平时不必付这笔钱）。
    # `eval/` 在 sys.path 上（见文件头第一段）。
    import golden_fixture  # noqa: E402
    _TITLES = golden_fixture.category_titles()
    for _cid, _fname in _NEED_FIX.items():
        _state = golden_fixture.fixture_state(_fname, _TITLES)
        if _state == "present":
            continue
        CASES = [c for c in CASES if c["id"] != _cid]
        _SKIPPED_IDS.append(_cid)
        _why = ("夹具不在位（先按授权串跑 scripts/migration/golden_write_fixture_20260925.sql）"
                if _state == "absent" else
                f"夹具在位检查读不到公开分类接口（{golden_fixture.UNREADABLE_TAG}）—— 不知道在不在，不跑")
        print(f"[skip] {_cid}: SKIP ({_why})", flush=True)
# 空分母（20260925）：全部被摘掉时**不许往下走**——本脚本的收尾统计会对空序列取
# min()/P50（ValueError），构造报告时还会除零；就算不炸，打印出来的也是"0/0 通过 = 100%"
# 那种静默的绿，而这一轮什么都没评。口径与 run_golden.py 逐字一致：退出码 2。
if not CASES:
    print("[full] ⚠ 一条用例都没剩下（被身份闸 / 真写闸 / 夹具闸摘干净了）—— 这一轮"
          "**没有评测任何东西**：空分母不是一个通过率，退出码 2（不是 0）", flush=True)
    sys.exit(2)
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

def spawn_case(case: dict, suffix: str = "") -> dict:
    """跑一条用例（独立子进程），返回结果 dict。

    `suffix` 追加到 trace 用例名上（20260924）：回归组首跑红要重跑一次，两次必须落
    **两份** trace——同名文件会把首跑那份覆盖掉，而"首跑为什么红"正是复跑要回答的。
    复跑同样走独立子进程（与首跑同一条链路），否则"两个跑法结论不同"那个老坑会以
    "进程内复跑 vs 隔离复跑"的形式重演。

    **20260925 改名（原 `run_case`）**：轮次驱动（发几轮、第 2 轮怎么带令牌）现在只有
    一份实现，在 `run_golden.run_case`，由子进程调用；本函数只是"spawn 一个进程"这层
    壳。两个不同的东西共用一个名字，正是"两个跑法悄悄各跑一套"最容易发生的地方。
    """
    cid = case["id"]
    json.dump(case, open(f"{TMPDIR}/{cid}.json", "w", encoding="utf-8"), ensure_ascii=False)
    argv = [".venv/bin/python", RUNNER, f"{TMPDIR}/{cid}.json", "eval/report/runs"]
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
    # tags 落进结果（20260924）：回归组的分组判据要用它，报告里也该看得见（哪条是回归题）
    r["tags"] = case.get("tags") or []
    results.append(r)
    ok = "PASS" if r["ok"] else "FAIL"
    fails = "; ".join(r.get("fails") or [])[:100]
    print(f"[{i}/{len(CASES)}] {ok} {cid:35s} {r.get('elapsed', 0):6.1f}s resets={r.get('resets', 0)} {fails}", flush=True)

# 回归组 FAIL **重跑一次再判**（20260924 用户拍板，动的是既有硬判纪律；口径与
# run_golden.py 逐字一致）：回归组要 100%，而它红的原因里混着方差——对首跑红的
# 回归用例各重跑一次，复跑仍红=真 FAIL，复跑绿=按方差放行但必须响（首跑红/复跑绿
# 两条都进报告、进汇总打印）。**只重跑回归组**：能力题本来就按通过率放宽。
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
# 回归组单列（20260924，口径与 run_golden.py 一致）：一条红即整轮红，但红先复跑一次再定论
print(f"回归组: {len(_REG) - len(_reg_bad)}/{len(_REG)}"
      + (f"  ⚠ 红：{_reg_bad}" if _reg_bad else "")
      + (f"  ⚠ 复跑才绿：{_reg_flaked}（首跑红，已按方差放行——首跑/复跑两条都在报告里）"
         if _reg_flaked else ""))

ts = time.strftime("%Y%m%d_%H%M%S")
# 指标口径与 run_golden.py **共用同一份实现**（20260924）：Wilson 区间与按 tag 分组都
# 从那边导入，不在这里抄第二份——两份判据/两份统计必然会漂移（build_request 那条
# "字段表只留一处"的教训是同一个道理，只是那次漂移的是请求体、这次会是数字）。
from run_golden import wilson_ci, by_tag_stats           # noqa: E402
_TAGS_MAP = {r["id"]: r["tags"] for r in results}
report = {"ts": ts, "corpus": "full", "total": len(CASES), "passed": len(CASES) - failed,
          "failed": failed, "latency_s": [r["elapsed"] for r in results],
          # 通过率（20260924 补）：这个跑法此前**没有** pass_rate 字段——只打印了
          # "N/M 通过"，报告里只有 passed/total 两个原子数，读的人要自己除。
          "pass_rate": round((len(CASES) - failed) / len(CASES), 4) if CASES else 0.0,
          "pass_rate_ci95": wilson_ci(len(CASES) - failed, len(CASES)),
          "by_tag": by_tag_stats(results, _TAGS_MAP),
          # 这个跑法就是全量的定义（逐条独立子进程），恒 True——留着是为了让两个
          # 报告文件的字段表形状一致（读报告的地方不必先问"这是哪个跑法写的"）。
          "full_run": True,
          "skipped_ids": list(_SKIPPED_IDS),
          # 其中「按设计不跑」的那批单列（口径同 run_golden.py）：真写用例默认不跑。
          "skipped_real_write_ids": list(_WRITE_SKIPPED),
          # 首跑红数（20260924）：failed 是复跑后的终判，这个留着首跑口径（差额=被吸收的红斑）
          "failed_first_run": failed_first,
          # 回归组块（20260924）：与 run_golden.py 同名字段——留档反查（golden_trace.
          # _run_verdicts）与"复跑才绿"的可见性都读它；此前只有 run_golden 的报告有这个块
          "regression": {"total": len(_REG),
                         "passed": len(_REG) - len(_reg_bad),
                         "failed_ids": _reg_bad,
                         "all_passed": not _reg_bad,
                         "flaked_ids": _reg_flaked,
                         # 跳过的用例里只有回归组值得单列（口径同 run_golden.py：跳过会改变
                         # 通过率分母，回归组的硬判 100% 更要看得见谁没跑）
                         "skipped_ids": [s for s in _SKIPPED_IDS
                                         if "regression" in _ALL_TAGS.get(s, [])]},
          # 这一轮的 trace 目录（20260922）：run_id 是本进程定的，子进程都落在它下面
          "trace_run": GOLDEN_RUN,
          "cases": results}
with open(f"eval/report/runs/{ts}.json", "w", encoding="utf-8") as f:
    json.dump(report, f, ensure_ascii=False, indent=1)
# `last_run.json` 的语义（20260924 定）：**最近一次全量跑**。这个跑法就是全量跑，
# 所以由它写（run_golden.py 那边加了 full_run 判据，非全量不再覆盖——此前一次
# `--only <单条>` 的调试跑会把它写成 total=1）。
with open("eval/report/last_run.json", "w", encoding="utf-8") as f:
    json.dump(report, f, ensure_ascii=False, indent=1)
print(f"报告: eval/report/runs/{ts}.json（并更新 eval/report/last_run.json）")
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
