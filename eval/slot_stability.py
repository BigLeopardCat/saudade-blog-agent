#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""槽稳定性读数：**同一条用例跑 N 遍，某一格取值是不是每一遍都一样**（20261020）。

## 为什么要有这个读数

`eval/run_golden.py` 每条用例**只跑一次**。一个七成可靠的槽（模型每次采样都重填，
有时候对、有时候把整条原话抄进去）在单发金标里是 **100% 绿**——而线上画给主人看的是
错的那一次。更糟的是"复跑按 flake 放行"（`run_golden.py` 头注里自己写着"同一条用例
红绿两态并存"）**正好把不稳定当噪声抹掉**：红的那一遍被复跑救回来，读数上看不见。

现场（20261020，trace `20261020T050425_1_r75832df`）：主人说「猫咪加条待办，亲亲泠月喵。」，
`create_dashboard_todo` 的 `text` 被填成**整条原话**（连命令部分一起），而不是「亲亲泠月喵」。
那条用例（`admin_todo_add_popup`）当时是绿的：它的判据 `args_from_input` 只判
「值是不是原话的子串」——**抓得住编造，抓不住整条照抄**（整条原话当然是自己的子串）。

## 它判什么、不判什么

判：**逐遍一致率** = 出现最多的那个取值占多少遍。1.0 = 每一遍都一样。
**不判对错**——"每一遍都抄错"是一致的，本读数不会红。对错是 gold 断言的事，两件必须
分开看（一致 + 对 ⇒ 稳且对；一致 + 错 ⇒ 稳但错，改判据；不一致 ⇒ 改行为）。

## 怎么跑（cd 仓根，需网络与 API key）

  .venv/bin/python eval/slot_stability.py --case admin_todo_add_popup \\
      --slot create_dashboard_todo.text --trials 12
  .venv/bin/python eval/slot_stability.py --case a --case b --trials 8 --json
  .venv/bin/python eval/slot_stability.py --case a --slot t.f --min-agreement 1.0   # 当闸用

**不进夜间、不进 CI**（用户 20261020 定）：它跑真 LLM，N 遍就是 N 次真对话，成本随 N
线性涨。它是"改提示词前后各跑一次"的仪器，不是常驻哨兵。报告落
`eval/report/slot_stability/`（与 `runs/` **分开**：那一个是 `golden_trace.prune` 的判据源、
`landing_gate` 按目录整扫，混进去会把本读数当成一次 golden 跑）。

## 两个实现上的硬约束（都是踩过的形状）

· **逐遍必须换线程坐标**（`GOLDEN_REP=i`，见 `eval/golden_thread.py`）：不换的话第 i 遍
  接着第 i-1 遍的 PG state 跑，读数看着正常、其实根本不是独立抽样。`ckpt_ab_driver.py`
  就是这么设的，这里照抄。
· **拿不到槽值不许静默跳过**：那一遍记成 `(缺席)` 并**算进分母**。跳过等于把"模型这一遍
  根本没产出那个参数"从读数里删掉——那正是最该看见的一遍。
"""
import argparse
import collections
import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "eval"))

import golden_trace  # noqa: E402  （run_id / 关档的环境变量名只有它一处）
import report_archive  # noqa: E402  （留档名的唯一事实源：毫秒戳 + O_EXCL）

GOLDEN_FILE = ROOT / "eval" / "golden" / "basic.jsonl"
RUNNER = ROOT / "eval" / "golden_case_runner.py"
TMPDIR = Path("/tmp/slot_stability_cases")
REPORTS_DIR = ROOT / "eval" / "report" / "slot_stability"
TIMEOUT = 180          # 与 `golden_full_run.py` 同一个数
MISSING = "(缺席)"     # 这一遍没产出那个参数（如实进分母，不跳过）


def load_cases(ids: list[str]) -> list[dict]:
    """按 id 取用例；**取不到就响亮退出**（静默跑空表 = 一份"全绿"的假读数）。"""
    want = set(ids)
    out: dict[str, dict] = {}
    for line in GOLDEN_FILE.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        c = json.loads(line)
        if c.get("id") in want:
            out[c["id"]] = c
    missing = [i for i in ids if i not in out]
    if missing:
        sys.exit(f"❌ 金标里没有这些用例：{', '.join(missing)}（{GOLDEN_FILE}）")
    return [out[i] for i in ids]


def payloads_of(result: dict) -> list[dict]:
    """一条 RESULT 里所有的确认载荷（逐轮收；顶层也收，两个产出者都写过）。"""
    out = list(result.get("confirm_payloads") or [])
    for rd in result.get("rounds") or []:
        out.extend(rd.get("confirm_payloads") or [])
    return [p for p in out if isinstance(p, dict)]


def extract_slot(result: dict, tool: str, field: str) -> str | None:
    """从一条 RESULT 里取 `tool` 那次调用的 `field` 参数值。

    ⚠️ 结构化参数在报告里**只活在** `confirm_payloads[*].specs[*].args`——
    `exec_rows` 不进报告、`tool_calls` 只有工具名（`run_golden.py` 的 `results.append`）。
    所以这条读数天然只覆盖**走确认卡的写操作**；走 execute 直执行的读工具没有这个出口。
    同一遍里出现多个同工具调用时取**第一个**（一张卡只装同一个本领的一件事）。
    """
    for p in payloads_of(result):
        for spec in p.get("specs") or []:
            if spec.get("tool") == tool:
                v = (spec.get("args") or {}).get(field)
                if v is not None:
                    return str(v)
    return None


def spawn(case: dict, rep: int, *, trace: bool, trace_run: str) -> dict:
    """跑一遍（独立子进程 + 本遍自己的线程坐标）。

    **trace 默认关**：本仪器的产物是那份 JSON 报告（逐遍的取值与 fails 都在里面），而
    `golden_traces/` 是别的工具在用的共享档（`golden_draft` / `trace_reconcile` 的读取面）。
    N 遍各落一个目录是纯噪声——更糟的是**每个子进程各解析一个 run_id**，N 遍会散成 N 个
    目录（`golden_full_run` 特意在父进程解析一次再经环境变量传下去，正是防这个）。
    要看某一遍为什么不一样时开 `--trace`，那时**整轮共用一个 run 目录**。
    """
    TMPDIR.mkdir(parents=True, exist_ok=True)
    casefile = TMPDIR / f"{case['id']}.json"
    casefile.write_text(json.dumps(case, ensure_ascii=False), encoding="utf-8")
    env = dict(os.environ)
    env["GOLDEN_REP"] = str(rep)
    if trace and trace_run:
        env[golden_trace.ENV_RUN] = trace_run
        env.pop(golden_trace.ENV_OFF, None)
    else:
        env[golden_trace.ENV_OFF] = "1"
    t0 = time.time()
    proc = subprocess.Popen(
        [sys.executable, str(RUNNER), str(casefile), str(REPORTS_DIR), f"_stab{rep}"],
        cwd=str(ROOT), env=env,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8",
    )
    try:
        out, err = proc.communicate(timeout=TIMEOUT)
    except subprocess.TimeoutExpired:
        proc.kill()
        out, err = proc.communicate()
        return {"id": case["id"], "rep": rep, "ok": False, "elapsed": round(time.time() - t0, 1),
                "fails": [f"超时 {TIMEOUT}s"], "error": (err or "")[-300:], "rounds": []}
    tail = out.strip().splitlines()[-1] if out.strip() else ""
    if not tail.startswith("RESULT "):
        return {"id": case["id"], "rep": rep, "ok": False, "elapsed": round(time.time() - t0, 1),
                "fails": [f"runner 无结果: {(err or out)[-200:]}"], "rounds": []}
    r = json.loads(tail[7:])
    r["rep"] = rep
    return r


def summarize(values: list[str]) -> dict:
    """一份取值表 → 众数、逐遍一致率、每格几遍。

    一致率的分母恒为**总遍数**（缺席的那几遍也算）——见模块头注。
    """
    n = len(values)
    cnt = collections.Counter(values)
    top, top_n = (cnt.most_common(1)[0] if cnt else (MISSING, 0))
    return {"trials": n, "values": dict(cnt), "mode": top, "mode_n": top_n,
            "agreement": (top_n / n) if n else None,
            "missing_n": cnt.get(MISSING, 0)}


def run_case(case: dict, tool: str, field: str, trials: int, verbose: bool,
             *, trace: bool, trace_run: str) -> dict:
    draws = []
    for rep in range(1, trials + 1):
        r = spawn(case, rep, trace=trace, trace_run=trace_run)
        val = extract_slot(r, tool, field)
        draws.append({"rep": rep, "ok": bool(r.get("ok")), "elapsed": r.get("elapsed"),
                      "value": val if val is not None else MISSING,
                      "fails": r.get("fails") or []})
        if verbose:
            mark = "·" if val is None else "✓"
            print(f"    第 {rep:>2} 遍 {mark} {val!r}", flush=True)
    return {"case": case["id"], "slot": f"{tool}.{field}", "draws": draws,
            **summarize([d["value"] for d in draws])}


def main() -> int:
    ap = argparse.ArgumentParser(description="同一用例跑 N 遍，看某一格取值稳不稳")
    ap.add_argument("--case", action="append", default=[], required=True,
                    help="用例 id（可重复；取不到即报错退出）")
    ap.add_argument("--slot", default="create_dashboard_todo.text",
                    help="`<工具名>.<参数名>`，如 create_dashboard_todo.text")
    ap.add_argument("--trials", type=int, default=8, help="每条的遍数（默认 8）")
    ap.add_argument("--min-agreement", type=float, default=None,
                    help="低于它退出码 1（不给就只出读数，不当闸）")
    ap.add_argument("--json", action="store_true", help="只出机器可读 JSON")
    ap.add_argument("--quiet", action="store_true", help="不打逐遍明细")
    ap.add_argument("--trace", action="store_true",
                    help="留下 golden trace（默认关；开了则整轮共用一个 run 目录）")
    args = ap.parse_args()

    if "." not in args.slot:
        sys.exit(f"❌ --slot 要写成 `<工具名>.<参数名>`，收到 {args.slot!r}")
    tool, field = args.slot.rsplit(".", 1)
    cases = load_cases(args.case)
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)

    # run_id 在**父进程**解析一次、经环境变量传给每个子进程：子进程各自解析会把一轮
    # 散成 N 个目录（`golden_full_run.py` 同一条注释）。
    trace_run = golden_trace.resolve_run_id() if args.trace else ""

    rows = []
    for case in cases:
        if not args.quiet:
            print(f"\n▶ {case['id']}  ← {case.get('user_input', '')[:40]!r}", flush=True)
        rows.append(run_case(case, tool, field, args.trials, verbose=not args.quiet,
                             trace=args.trace, trace_run=trace_run))

    ts = time.strftime("%Y%m%d_%H%M%S")
    with report_archive.open_archive(REPORTS_DIR, ext=".json") as (path, fh):
        json.dump({"ts": ts, "slot": args.slot, "trials": args.trials, "rows": rows},
                  fh, ensure_ascii=False, indent=1)

    if args.json:
        print(json.dumps({"slot": args.slot, "rows": rows}, ensure_ascii=False))
    else:
        print(f"\n══ 逐遍一致率（槽 {args.slot}）══")
        for r in rows:
            print(f"  {r['case']:<32} {r['agreement']:.0%}  "
                  f"（{r['mode_n']}/{r['trials']} 遍是 {r['mode']!r}）")
            for v, n in sorted(r["values"].items(), key=lambda kv: -kv[1]):
                print(f"      ×{n}  {v!r}")
        print(f"\n  留档：{Path(path).relative_to(ROOT)}")

    if args.min_agreement is not None:
        bad = [r["case"] for r in rows
               if r["agreement"] is None or r["agreement"] < args.min_agreement]
        if bad:
            print(f"\n❌ 低于 {args.min_agreement:.0%}：{', '.join(bad)}")
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
