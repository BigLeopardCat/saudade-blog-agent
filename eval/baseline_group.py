# -*- coding: utf-8 -*-
"""按 tag 把**多次**跑法汇成一份可提交的基线（20260927 主线批 A / 1C）。

**为什么需要这个文件**：单跑一次的通过率，对「多步 + 未具名指称」这类**采样敏感**的形状
毫无信息量。实测同一个 4 条用例的组连跑 6 次：4/4、3/4、4/4、2/4、3/4、4/4——用其中任何
一次当基线，结论会正好相反（"这组绿了" vs "这组一半红"）。基线要的是 **n 次里逐条各绿
几次**＋ Wilson 区间，而不是又一个百分比。

**为什么另起一个脚本**：`run_golden.py` 的产出是"**一次**跑"的报告，聚合是另一件事
（读多份报告 → 一张表）。**刻意不做"自动挑最近 N 份"**：挑哪几份是判据不是实现细节——
必须同一档 engine、同一组 tag、同一份语料，自动挑会把换档前后的报告混进同一张表，
而这两者的数字不可平均（批 B 的 ×4 档对照正是要在**同组**上比不同档）。

**收进来的门槛（不满足就整份跳过并打印原因）**：
  · 报告里**每一条**用例都带该 tag —— 混着组外用例时，"这组的通过率"与"这次跑的通过率"
    是两个数，取哪个都是静默的口径漂移；
  · engine 与先前收进的那份一致 —— 不同档的样本不能平均（报告没这个字段 = `unknown`，
    与 `text` 也不相等，同样跳过）；
  · 该用例在某次里**缺席** = 未评估，不计入它的分母（跳过的用例会改变通过率分母，
    这是既有纪律，见 run_golden.py 的 skipped_ids）。

用法（仓库根 cwd）：
  .venv/bin/python eval/baseline_group.py --tag multi_step \\
      --out eval/report/baseline_20260927_multi_step.json eval/report/runs/2026*.json

`--out` 落在 `eval/report/baseline_*.json`：`.gitignore` 里 `eval/report/*` 被忽略，只放行
`baseline_*.json`——基线是**要进仓库**的东西（对照起点），跑法报告不是。
"""
import argparse
import glob
import io
import json
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", line_buffering=True)
sys.path.insert(0, "eval")

from run_golden import wilson_ci  # noqa: E402


def load_report(path: str) -> dict | None:
    """读一份跑法报告；读不到/不是报告就返回 None（**不抛**——一批 glob 里混着别的 json 是常态）。"""
    try:
        with open(path, encoding="utf-8") as f:
            rep = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(rep, dict) or not isinstance(rep.get("cases"), list):
        return None
    return rep


def select(reports: list[tuple[str, dict]], tag: str) -> tuple[list, list]:
    """按 tag 与 engine 两道门槛筛报告 → (收进的, [(路径, 原因)])。

    engine 的一致性判据是"与**第一份被收进的**相同"。用集合而不是"全都等于 text"：
    基线跑哪一档是调用方的事，这里只保证**同一张表里的样本同档**。
    """
    kept: list = []
    skipped: list = []
    engine: str | None = None
    for path, rep in reports:
        ids = [c.get("id") for c in rep["cases"]]
        outside = [c.get("id") for c in rep["cases"] if tag not in (c.get("tags") or [])]
        if outside:
            skipped.append((path, f"含组外用例 {outside[:3]}（另 {max(0, len(outside) - 3)} 条）"
                                  "—— 混着别的用例时这组的通过率与整跑通过率是两个数"))
            continue
        eng = str(rep.get("engine") or "unknown")
        if engine is None:
            engine = eng
        elif eng != engine:
            skipped.append((path, f"engine={eng} 与已收进的 {engine} 不同档"
                                  "—— 不同档的样本不能平均"))
            continue
        if not ids:
            skipped.append((path, "报告里一条用例都没有"))
            continue
        kept.append((path, rep))
    return kept, skipped


def aggregate(kept: list[tuple[str, dict]], tag: str) -> dict:
    """(路径, 报告) 列表 → 逐条计数 + 整组通过率与区间。"""
    per_case: dict[str, dict] = {}
    engines = set()
    for _path, rep in kept:
        engines.add(str(rep.get("engine") or "unknown"))
        for c in rep["cases"]:
            b = per_case.setdefault(c["id"], {"runs": 0, "passed": 0, "fails": [],
                                              "elapsed": [], "tags": c.get("tags") or []})
            b["runs"] += 1
            if c.get("final_ok", c.get("ok")):
                b["passed"] += 1
            else:
                b["fails"].append({"report": _path, "fails": c.get("fails") or [],
                                   "trace": c.get("trace")})
            if isinstance(c.get("elapsed"), (int, float)):
                b["elapsed"].append(c["elapsed"])
    runs = sum(b["runs"] for b in per_case.values())
    passed = sum(b["passed"] for b in per_case.values())
    for b in per_case.values():
        el = sorted(b["elapsed"])
        b["elapsed"] = {"count": len(el), "min": round(el[0], 1) if el else None,
                        "p50": round(el[len(el) // 2], 1) if el else None,
                        "max": round(el[-1], 1) if el else None}
        b["pass_rate"] = round(b["passed"] / b["runs"], 4) if b["runs"] else 0.0
    return {"tag": tag, "engine": sorted(engines)[0] if len(engines) == 1 else "?",
            "reports": [p for p, _ in kept], "runs": len(kept),
            "case_runs": runs, "case_passed": passed,
            "pass_rate": round(passed / runs, 4) if runs else 0.0,
            "pass_rate_ci95": wilson_ci(passed, runs),
            # 逐条在前：整个脚本存在的理由就是这一块（"哪一条在几次里红了几次"）
            "cases": per_case}


def main() -> int:
    ap = argparse.ArgumentParser(description="按 tag 汇总多次跑法 → 基线 JSON")
    ap.add_argument("--tag", required=True, help="用例族标签（如 multi_step）")
    ap.add_argument("--out", required=True, help="基线落盘路径（eval/report/baseline_*.json）")
    ap.add_argument("--ts", default="", help="写进基线的时刻戳（默认不写）")
    ap.add_argument("reports", nargs="+", help="跑法报告路径/glob")
    args = ap.parse_args()

    paths: list[str] = []
    for pat in args.reports:
        hits = sorted(glob.glob(pat)) if any(ch in pat for ch in "*?[") else [pat]
        if not hits:
            print(f"[warn] 没匹配到文件：{pat}", flush=True)
        paths += hits
    pairs = [(p, r) for p in paths if (r := load_report(p)) is not None]
    if len(pairs) != len(paths):
        print(f"[info] {len(paths) - len(pairs)} 份不是跑法报告（已忽略）", flush=True)
    kept, skipped = select(pairs, args.tag)
    for path, why in skipped:
        print(f"[skip] {path}: {why}", flush=True)
    # 逐份打印用例条数：**允许部分跑**（只跑了组内某一条的报告也是那条用例的合法样本，
    # 复跑红斑正是这么来的），但"这一份跑了组里几条"必须看得见——否则 n 的来路不明。
    for path, rep in kept:
        print(f"[收] {path}: {len(rep['cases'])} 条用例", flush=True)
    if not kept:
        print(f"[基线] 一份报告都没收进（tag={args.tag}）——**没生成文件**，空基线比没有更坏",
              flush=True)
        return 2

    out = aggregate(kept, args.tag)
    if args.ts:
        out["ts"] = args.ts
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print(f"[基线] tag={out['tag']} engine={out['engine']} 跑 {out['runs']} 次 / "
          f"{out['case_runs']} 次用例：{out['case_passed']} 绿 "
          f"= {out['pass_rate']:.3f}（Wilson 95% {out['pass_rate_ci95'][0]:.3f}–"
          f"{out['pass_rate_ci95'][1]:.3f}）", flush=True)
    for cid, b in out["cases"].items():
        flag = "" if b["passed"] == b["runs"] else "  ⚠"
        print(f"  {b['passed']}/{b['runs']}  {cid}{flag}  p50={b['elapsed']['p50']}s", flush=True)
    print(f"[基线] 落盘：{args.out}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
