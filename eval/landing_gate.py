# -*- coding: utf-8 -*-
"""Archived golden-run diagnostics and statistical comparisons.

Current release decisions use eval.release_readiness and release_profile.json.
This module retains the metrics needed to compare archived runner reports."""
import argparse
import glob
import json
import math
import os
import sys

# ── 三个数（唯一取值处；夜间、报告、复审单都从这里读，不在各处重抄）────────────────
FLOOR = 0.78          # 地板：采样层下界低于它 ⇒ 当夜置红（事故闸，不是目标）
ENTRY = 0.90          # 当前档位：达档只记，不置红；连 RAISE_NIGHTS 夜达档 ⇒ 抬档

TARGET = 0.95         # 落地目标（主人定的整体线）
RAISE_STEP = 0.05     # 抬档步长
RAISE_NIGHTS = 3      # 连几夜达档才抬档（单夜达标是运气，连三夜才是水平）
Z95 = 1.96
AB_TOL = 0.005        # A/B：下界回退不超过这个量算"在噪声内"（半个百分点）

RUNS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "report", "runs")
FULL_RUN_MIN_CASES = 100   # 「全量跑」的判据（`--only` 的调试跑不进红榜：分母不可比）

# Archived diagnostics retain separate overall, domain and visible-fallback thresholds.
DOMAIN_HINT = 0.90      # 技能面下界 ≥ 它算健康；< FLOOR 算事故（只在 readiness 里判）
DOMAIN_MIN_ROUNDS = 20  # 一个面至少这么多轮次才判下界（跨夜聚合的窗口里）
HARM_HINT = 0.01        # 用户可见兜底占比 ≥ 它 ⇒ 记黄
HARM_FLOOR = 0.05       # ≥ 它 ⇒ **事故**（当夜置红）。已知最差一夜 6/151=4.0% 仍在
                        # 黄区 ⇒ 这条闸只抓"gate 整片误伤"，不抓正常波动（同 FLOOR 的取向）
READY_NIGHTS = 5        # 企业落地判据看最近几夜



def wilson_ci(passed: int, total: int, z: float = Z95) -> list:
    """Return a Wilson confidence interval for a Bernoulli success proportion."""
    if total <= 0:
        return [0.0, 0.0]
    p = passed / total
    d = 1 + z * z / total
    center = (p + z * z / (2 * total)) / d
    half = z * ((p * (1 - p) / total + z * z / (4 * total * total)) ** 0.5) / d
    return [round(max(0.0, center - half), 4), round(min(1.0, center + half), 4)]


def min_n_zero_fail(target: float, z: float = Z95) -> int:
    """Return the minimum zero-failure sample size needed for the target lower bound."""
    return int(math.ceil(target * z * z / (1 - target)))


def fallback_resets(result: dict) -> list[str]:
    """Return reasons for archived text-only terminal fallbacks.

    Decision resets invalidate provisional plans and are not necessarily
    user-visible failures; archived unscoped resets remain conservatively counted."""
    scopes = result.get("reset_scopes")
    if scopes is None:
        return [str(r) for r in (result.get("resets_reasons") or [])] \
            or [""] * int(result.get("resets") or 0)
    reasons = result.get("fallback_reasons")
    if reasons is None:  # 形状对齐得上一半（有 scopes、没有逐条理由）时按计数补位
        return [""] * sum(1 for s in scopes if s == "text")
    return list(reasons)

def harm_stats(cases: list, *, floor: float = HARM_FLOOR, hint: float = HARM_HINT) -> dict:
    """Count evaluated turns that exposed a terminal fallback to the user.

    Do not count decision resets followed by a successful answer as user-visible harm."""
    total = len(cases)
    ids = [c.get("id") for c in cases if fallback_resets(c)]
    rate = len(ids) / total if total else 0.0
    state = "incident" if rate >= floor else ("hint" if rate >= hint else "ok")
    return {"rounds": total, "hit": len(ids), "ids": ids, "rate": round(rate, 4),
            "ci95": wilson_ci(len(ids), total), "state": state,
            "floor": floor, "hint": hint,
            "criterion": f"用户收到兜底道歉（scope=text）的轮次占比 < {hint} 记绿"
                         f"、< {floor} 记黄、≥ {floor} 置红"}


def domain_rates(cases: list, *, hint: float = DOMAIN_HINT, floor: float = FLOOR,
                 min_rounds: int = DOMAIN_MIN_ROUNDS) -> list:
    """Compute per-tag success proportions and Wilson intervals for the sampled cases.

    A case may belong to multiple tags; these groups are not disjoint populations."""
    buckets: dict = {}
    for c in cases:
        if "regression" in (c.get("tags") or []):
            continue
        for tag in set(c.get("tags") or []):
            b = buckets.setdefault(tag, {"rounds": 0, "red": 0})
            b["rounds"] += 1
            if not _final_ok(c):
                b["red"] += 1
    rows = []
    for tag, b in buckets.items():
        n, k = b["rounds"], b["red"]
        ci = wilson_ci(n - k, n)
        rows.append({"tag": tag, "rounds": n, "red": k, "passed": n - k,
                     "point": round((n - k) / n, 4) if n else 0.0,
                     "ci95": ci, "lower": ci[0], "upper": ci[1],
                     "judged": n >= min_rounds,
                     "floor_ok": ci[0] >= floor, "hint_ok": ci[0] >= hint})
    rows.sort(key=lambda r: (r["lower"], r["tag"]))
    return rows


def readiness(runs_dir: str = RUNS_DIR, *, nights: int = READY_NIGHTS,
              min_cases: int = FULL_RUN_MIN_CASES, entry: float = ENTRY,
              target: float = TARGET, domain_hint: float = DOMAIN_HINT,
              harm_floor: float = HARM_FLOOR, harm_hint: float = HARM_HINT) -> dict:
    """Assess archived multi-run targets across overall, domain and fallback metrics.

    Keep insufficient sample sizes and missing report evidence explicit.
    Passing this historical profile does not establish current release readiness."""
    reps = full_reports(runs_dir, limit=nights, min_cases=min_cases)
    cases: list = []
    lows: list = []
    for rep in reps:
        cases.extend(rep.get("cases") or [])
        land = rep.get("landing") or verdict(rep.get("cases") or [],
                                             (rep.get("regression") or {}).get("failed_ids") or [])
        lows.append(land["sampled"]["lower"])
    doms = domain_rates(cases, hint=domain_hint)
    judged = [r for r in doms if r["judged"]]
    weak = [r for r in judged if not r["floor_ok"]]
    hint_only = [r for r in judged if r["floor_ok"] and not r["hint_ok"]]
    harm = harm_stats(cases, floor=harm_floor, hint=harm_hint)
    enough = len(reps) >= nights
    criteria = [
        {"key": "overall", "name": f"总下界（最近 {nights} 夜每夜）",
         "ok": enough and bool(lows) and all(lo >= entry for lo in lows),
         "strong": enough and bool(lows) and all(lo >= target for lo in lows),
         "detail": f"下界 {['%.3f' % x for x in lows]}，档位 {entry} / 目标 {target}"
                   + ("" if enough else f"（全量历史只有 {len(reps)} 夜，不够 {nights} 夜）")},
        {"key": "domains", "name": "技能面下界（跨夜聚合）",
         "ok": bool(judged) and not weak,
         "strong": bool(judged) and not weak and not hint_only,
         "detail": f"判定 {len(judged)} 个面；低于地板 {len(weak)} 个"
                   + (f"（{[ (r['tag'], r['lower']) for r in weak ]}）" if weak else "")
                   + f"；{FLOOR}–{domain_hint} 之间 {len(hint_only)} 个"
                   + (f"（{[ (r['tag'], r['lower']) for r in hint_only ]}）" if hint_only else "")},
        {"key": "harm", "name": "用户可见兜底（用户真收到道歉）",
         "ok": harm["rate"] < harm_floor,
         "strong": harm["rate"] < harm_hint,
         "detail": f"{harm['hit']}/{harm['rounds']} 轮 = {harm['rate']:.2%}"
                   f"（黄 ≥{harm_hint:.0%}、红 ≥{harm_floor:.0%}）"
                   + (f"；最频繁：{harm['ids'][:5]}" if harm["ids"] else "")},
    ]
    return {"nights": len(reps), "nights_wanted": nights, "ready": all(c["ok"] for c in criteria),
            "strong": all(c["strong"] for c in criteria), "criteria": criteria,
            "domains": doms, "weak_domains": weak, "hint_domains": hint_only,
            "harm": harm, "lows": lows, "cases": len(cases)}


def _final_ok(case: dict) -> bool:
    """一条用例的终判：复跑过就以复跑为准（与 `run_golden` 的门禁同一口径）。"""
    return bool(case.get("final_ok", case.get("ok")))


def _rate(cases: list) -> dict:
    """一组用例的采样层读数（能力题 = 非回归组；回归组由硬层管）。"""
    ability = [c for c in cases if "regression" not in (c.get("tags") or [])]
    failed_ids = [c["id"] for c in ability if not _final_ok(c)]
    total, passed = len(ability), len(ability) - len(failed_ids)
    ci = wilson_ci(passed, total)
    return {"total": total, "passed": passed, "failed_ids": failed_ids,
            "point": round(passed / total, 4) if total else 0.0,
            "ci95": ci, "lower": ci[0], "upper": ci[1]}


def verdict(cases: list, regression_failed_ids: list, *,
            floor: float = FLOOR, entry: float = ENTRY, target: float = TARGET,
            z: float = Z95) -> dict:
    """Assess one archived run from its case outcomes and regression failures.

    Keep fixed regressions separate from sampled cases. A small sample can
    indicate collapse, but cannot certify the target confidence bound."""
    s = _rate(cases)
    total, lower = s["total"], s["lower"]
    need_n = min_n_zero_fail(entry, z)
    if total < need_n:
        # 样本不足：判不了档位；但"点估计都够不上地板"没有第二种解释。注意 `total == 0`
        # （空分母）不在此列——那由 `run_golden` 的退出码 2 管，且"没有一条被测到"与
        # "每条都红"是两件事（点估计在 total=0 时按 0.0 返回，别让它冒充塌方）。
        state, basis = (("collapse", "point") if total and s["point"] < floor
                        else ("underpowered", ""))
    elif lower < floor:
        state, basis = "collapse", "lower"
    elif lower >= target:
        state, basis = "at_target", ""
    elif lower >= entry:
        state, basis = "pass", ""
    else:
        state, basis = "below_entry", ""
    # 企业落地的后两条在这里只**记录**：面判据要跨夜聚合才有证明力（单夜每面 3–5 条，
    # `4/4` 的下界 0.51 全是噪声），所以最弱面的**判决**住在 `readiness()`；用户可见兜底
    # 是逐轮事实、单夜可判，所以它带着 `state`（`incident` 由 run_golden 置红）。
    doms = [r for r in domain_rates(cases, min_rounds=3) if r["rounds"] >= 3]
    return {
        "hard": {
            # 硬层这一格只装"golden 报告里看得见的"那半（回归组）；离线套件与探针在
            # nightly 脚本里各自置红，不在这里重算一遍（第二份判据）。
            "name": "确定性层（回归组；离线套件与探针在 nightly 里各自判）",
            "regression_failed_ids": list(regression_failed_ids),
            "ok": not regression_failed_ids,
            "criterion": "0 红（不给百分比——这一层的红没有频率含义）",
        },
        "sampled": dict(s, name="采样层（能力题）", entry=entry, floor=floor,
                        target=target, min_n_for_entry=need_n,
                        underpowered=state == "underpowered", state=state,
                        # 塌方判据用的是下界还是上界（小样本走后者）——日志/复审单据此措辞
                        collapse_basis=basis,
                        criterion=f"Wilson 95% 下界：< {floor} 置红，≥ {entry} 记达档"),
        "distance_to_target": round(max(0.0, target - lower), 4),
        "raise_ready": state in ("pass", "at_target"),
        "floor": floor, "entry": entry, "target": target,
        # Record domain rates; readiness performs the multi-run domain assessment.
        "domains": {
            "weakest": doms[:5],
            "note": "单夜每面只有 3–5 条用例，下界没有证明力 ⇒ 这里只记录最弱的几面，"
                    "面判据（< 地板 = 事故）住在 `readiness()` 的跨夜聚合里",
        },
        "harm": harm_stats(cases),
    }


def describe(v: dict) -> str:
    """一行话把判定说清（日志、复审单、报告三处共用同一句话）。"""
    s, h = v["sampled"], v["hard"]
    head = ("硬层 ✅" if h["ok"] else f"硬层 ❌ 回归组红 {h['regression_failed_ids']}")
    if s["state"] == "underpowered":
        return (f"{head}；采样层 ⏸ 样本不足（{s['passed']}/{s['total']}，"
                f"要判档位 {s['entry']} 至少 {s['min_n_for_entry']} 条）⇒ 不判、不置红")
    mark = {"collapse": "❌", "below_entry": "◐", "pass": "✅", "at_target": "★"}[s["state"]]
    word = {"below_entry": "未到档位", "pass": "达档", "at_target": "达到目标"}.get(s["state"])
    if s["state"] == "collapse":
        word = (f"小样本但点估计 {s['point']:.3f} 已低于地板 {s['floor']} ⇒ 事故"
                if s.get("collapse_basis") == "point"
                else f"下界低于地板 {s['floor']} ⇒ 事故")
    hm = v.get("harm") or {}
    tail = ""
    if hm.get("hit"):
        tail = (f"；用户可见兜底 {hm['hit']}/{hm['rounds']} 轮 = {hm['rate']:.2%}"
                f"（黄 ≥{hm['hint']:.0%} / 红 ≥{hm['floor']:.0%}）")
    return (f"{head}；采样层 {mark} {s['passed']}/{s['total']}"
            f"（点估计 {s['point']:.3f}，下界 {s['lower']:.3f}）{word}；"
            f"档位 {s['entry']} / 目标 {s['target']}（还差 {v['distance_to_target']:.3f}）{tail}")


def ab_compare(before: list, after: list, *, tol: float = AB_TOL, z: float = Z95) -> dict:
    """Compare archived before/after runs without resampling failed cases.

    Use repeated-run evidence and report sample limitations rather than
    treating a single higher point estimate as a reliable improvement."""
    def _flatten(side: list) -> list:
        out: list = []
        for item in side:
            out.extend(item.get("cases") if isinstance(item, dict) and "cases" in item else item)
        return out

    cases_b, cases_a = _flatten(before), _flatten(after)
    rb, ra = _rate(cases_b), _rate(cases_a)
    need_n = min_n_zero_fail(ENTRY, z)
    if rb["total"] < need_n or ra["total"] < need_n:
        state = "underpowered"
    elif ra["lower"] < rb["lower"] - tol:
        state = "regressed"
    else:
        state = "no_regression"
    # 逐条红率对比（只在两边都跑过的用例上比；n 小时只报数不下判决）
    per: dict = {}
    for tag, cs in (("before", cases_b), ("after", cases_a)):
        for c in cs:
            e = per.setdefault(c["id"], {"id": c["id"], "before": [0, 0], "after": [0, 0],
                                         "tags": c.get("tags") or []})
            e[tag][1] += 1
            if not _final_ok(c):
                e[tag][0] += 1
    both = [e for e in per.values() if e["before"][1] and e["after"][1]]
    for e in both:
        e["before_rate"] = round(e["before"][0] / e["before"][1], 4)
        e["after_rate"] = round(e["after"][0] / e["after"][1], 4)
        e["delta"] = round(e["after_rate"] - e["before_rate"], 4)
    rose = sorted([e for e in both if e["delta"] > 0], key=lambda e: -e["delta"])
    fell = sorted([e for e in both if e["delta"] < 0], key=lambda e: e["delta"])
    return {
        "verdict": state,
        "before": rb, "after": ra,
        "delta_lower": round(ra["lower"] - rb["lower"], 4),
        "tol": tol,
        "per_case_rose": rose, "per_case_fell": fell,
        "cases_compared": len(both),
        "note": ("下界没退（含 %.3f 容差）⇒ 可落地" % tol) if state == "no_regression"
                else ("下界退 %.3f ⇒ 不可落地" % (rb["lower"] - ra["lower"])) if state == "regressed"
                else f"样本不足（前后各需 ≥{need_n} 条采样）⇒ 不判",
    }


def chronic_reds(runs_dir: str = RUNS_DIR, *, min_cases: int = FULL_RUN_MIN_CASES,
                 min_runs: int = 1) -> list:
    """Compute per-case terminal failure rates from full archived reports.

    Exclude partial diagnostic runs; a single run cannot establish a recurring failure."""
    total: dict = {}
    final_red: dict = {}
    first_red: dict = {}
    tags: dict = {}
    for path in sorted(glob.glob(os.path.join(runs_dir, "*.json"))):
        try:
            with open(path, encoding="utf-8") as f:
                rep = json.load(f)
        except (OSError, ValueError):
            continue
        cases = rep.get("cases") or []
        if len(cases) < min_cases:
            continue
        for c in cases:
            cid = c.get("id")
            if not cid:
                continue
            total[cid] = total.get(cid, 0) + 1
            tags.setdefault(cid, c.get("tags") or [])
            if not _final_ok(c):
                final_red[cid] = final_red.get(cid, 0) + 1
            if not c.get("ok"):
                first_red[cid] = first_red.get(cid, 0) + 1
    rows = []
    for cid, n in total.items():
        if n < min_runs or not final_red.get(cid):
            continue
        k = final_red[cid]
        rows.append({"id": cid, "runs": n, "red": k,
                     "first_run_red": first_red.get(cid, 0),
                     "rate": round(k / n, 4), "ci95": wilson_ci(k, n),
                     "tags": tags.get(cid) or []})
    rows.sort(key=lambda r: (-r["red"], -r["rate"], r["id"]))
    return rows


def chronic_map(runs_dir: str = RUNS_DIR, **kw) -> dict:
    """`{id: (红次数, 全量次数)}`——复审单给每条红印"历史 k/n"，让归类从猜变成读。"""
    return {r["id"]: (r["red"], r["runs"]) for r in chronic_reds(runs_dir, **kw)}


def full_reports(runs_dir: str = RUNS_DIR, *, limit: int = 0,
                 min_cases: int = FULL_RUN_MIN_CASES) -> list:
    """按时间序读回**全量**报告（老的在前）；`limit>0` 时只要最近 N 份。

    文件名是 `%Y%m%d_%H%M%S`（字典序 == 时间序）⇒ **倒着读、够了就停**：盘上有 852 份
    报告，只为了最近 3 份而全量 load 一遍是白花的秒级开销（这个函数在夜间每次跑都会被叫）。
    """
    reps = []
    for path in sorted(glob.glob(os.path.join(runs_dir, "*.json")), reverse=True):
        try:
            with open(path, encoding="utf-8") as f:
                rep = json.load(f)
        except (OSError, ValueError):
            continue
        if len(rep.get("cases") or []) >= min_cases:
            reps.append(rep)
            if limit and len(reps) >= limit:
                break
    reps.reverse()
    return reps


def raise_hint(runs_dir: str = RUNS_DIR, *, nights: int = RAISE_NIGHTS) -> dict:
    """Check whether every one of the latest required full runs meets the target.

    Missing evidence and too few full runs remain unverified."""
    reps = full_reports(runs_dir, limit=nights)
    if len(reps) < nights:
        return {"ready": False, "lows": [], "entry": ENTRY, "target": TARGET,
                "reason": f"全量历史只有 {len(reps)} 次，不够 {nights} 次"}
    lows = []
    for rep in reps:
        land = rep.get("landing") or verdict(rep.get("cases") or [],
                                             (rep.get("regression") or {}).get("failed_ids") or [])
        lows.append(land["sampled"]["lower"])
    ready = all(lo >= ENTRY for lo in lows) and ENTRY < TARGET
    return {"ready": ready, "lows": lows, "entry": ENTRY,
            "next": round(min(TARGET, ENTRY + RAISE_STEP), 4),
            "target": TARGET,
            "reason": (f"最近 {nights} 夜下界 {lows} 全部 ≥ {ENTRY} ⇒ 可抬到 "
                       f"{round(min(TARGET, ENTRY + RAISE_STEP), 4)}"
                       if ready else f"最近 {nights} 夜下界 {lows}，未夜夜达档 {ENTRY}")}


def main() -> int:
    ap = argparse.ArgumentParser(description="落地指标 / 慢性红榜 / A-B 落地判据（20261001）")
    ap.add_argument("--red-rank", action="store_true",
                    help="打印慢性红榜（按贡献的红次数排序 = 优化工单）")
    ap.add_argument("--raise-hint", action="store_true", help="档位该不该抬（看最近 N 夜）")
    ap.add_argument("--readiness", action="store_true",
                    help="企业落地判据（三条：总下界夜夜达档 / 无面低于地板 / 无用户可见兜底事故）")
    ap.add_argument("--nights", type=int, default=READY_NIGHTS, help="readiness 看最近几夜")
    ap.add_argument("--ab", nargs=2, metavar=("BEFORE", "AFTER"),
                    help="A/B 落地判据：各给一个 glob（如 'runs/*_before.json'）")
    ap.add_argument("--runs", default=RUNS_DIR, help="历史报告目录")
    ap.add_argument("--min-cases", type=int, default=FULL_RUN_MIN_CASES,
                    help="多长的报告才算一次全量跑（默认 100）")
    ap.add_argument("--min-runs", type=int, default=1, help="至少出现过几次才上榜")
    ap.add_argument("--limit", type=int, default=40, help="最多列几条 / A/B 各取最近几份")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    args = ap.parse_args()

    if args.ab:
        def _load(pat: str) -> list:
            out = []
            for p in sorted(glob.glob(pat)):
                try:
                    with open(p, encoding="utf-8") as f:
                        out.append(json.load(f))
                except (OSError, ValueError):
                    continue
            return out[-args.limit:] if args.limit else out
        res = ab_compare(_load(args.ab[0]), _load(args.ab[1]))
        if args.json:
            print(json.dumps(res, ensure_ascii=False, indent=1))
            return 0 if res["verdict"] != "regressed" else 1
        print(f"# A/B 落地判据：{res['verdict']} —— {res['note']}")
        for side in ("before", "after"):
            r = res[side]
            print(f"  {side:6s} {r['passed']}/{r['total']} 点估计 {r['point']:.3f} "
                  f"下界 {r['lower']:.3f}（区间 {r['ci95'][0]:.3f}–{r['ci95'][1]:.3f}）")
        if res["per_case_rose"]:
            print(f"  ⚠ 逐条红率**上升**（{len(res['per_case_rose'])} 条，聚合可能把它平均掉）：")
            for e in res["per_case_rose"][:15]:
                print(f"      {e['before_rate']:.2f} → {e['after_rate']:.2f}  {e['id']}")
        if res["per_case_fell"]:
            print(f"  ↓ 逐条红率下降 {len(res['per_case_fell'])} 条")
        return 0 if res["verdict"] != "regressed" else 1

    if args.raise_hint:
        h = raise_hint(args.runs)
        print(f"# 档位 {h['entry']} / 目标 {h['target']}：{h['reason']}")
        return 0

    if args.readiness:
        r = readiness(args.runs, nights=args.nights, min_cases=args.min_cases)
        if args.json:
            print(json.dumps(r, ensure_ascii=False, indent=1))
            return 0
        print(f"# 企业落地判据（最近 {r['nights']}/{r['nights_wanted']} 夜，"
              f"{r['cases']} 条用例）")
        print(f"# ready={'✅ 三条都成立' if r['ready'] else '❌ 还没到'}；"
              f"strong（够到 hint/target 级）={'✅' if r['strong'] else '❌'}\n")
        for c in r["criteria"]:
            mark = "✅" if c["ok"] else "❌"
            star = " ★" if c["strong"] else ""
            print(f"  {mark} {c['name']}{star}")
            print(f"      {c['detail']}")
        weak = [d for d in r["domains"] if d["judged"]]
        if weak:
            print(f"\n# 技能面下界（判定 {len(weak)} 个，升序）：")
            for d in weak[:12]:
                flag = ("❌" if not d["floor_ok"] else ("◐" if not d["hint_ok"] else "✅"))
                print(f"  {flag} {d['tag']:<20} {d['passed']:>3}/{d['rounds']:<4}"
                      f" = {d['point']:.3f}  下界 {d['lower']:.3f}"
                      f"  区间 [{d['ci95'][0]:.2f}, {d['ci95'][1]:.2f}]")
        skipped = len(r["domains"]) - len(weak)
        if skipped:
            print(f"  （另有 {skipped} 个面轮次 < {DOMAIN_MIN_ROUNDS}，不判）")
        print(f"\n# 读法：❌面 = 下界 < 地板 {FLOOR}（这个面按企业标准不达标）；"
              f"◐面 = {FLOOR}–{DOMAIN_HINT} 之间。")
        print("# 这三条**就是「能不能落地」的判据**，但**不并入夜间退出码**：面判据要跨夜聚合，"
              "而夜里那道红是 `FLOOR` 的事故语义（每晚必红就不叫事故了）。"
              "什么时候把它升成门禁，见模块头注。")
        return 0

    if not args.red_rank:
        ap.print_help()
        return 0
    rows = chronic_reds(args.runs, min_cases=args.min_cases, min_runs=args.min_runs)
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=1))
        return 0
    runs = len(full_reports(args.runs, min_cases=args.min_cases))
    tot_red = sum(r["red"] for r in rows)
    print(f"# 慢性红榜（{runs} 次全量跑）")
    print(f"# 地板 {FLOOR}（置红）/ 档位 {ENTRY} / 目标 {TARGET}"
          f"（零失败要声称它需 {min_n_zero_fail(TARGET)} 条样本）")
    print(f"# 上榜 {len(rows)} 条，合计 {tot_red} 次红\n")
    print(f"{'红/次数':>10}   {'95%区间':<14} {'首跑红':>6}  用例")
    for r in rows[:args.limit]:
        lo, hi = r["ci95"]
        print(f"{r['red']:>4}/{r['runs']:<5}  [{lo:.2f}, {hi:.2f}]      "
              f"{r['first_run_red']:>4}   {r['id']}")
    if len(rows) > args.limit:
        print(f"... 另有 {len(rows) - args.limit} 条（--limit 调大）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
