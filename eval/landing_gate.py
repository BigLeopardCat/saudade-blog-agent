# -*- coding: utf-8 -*-
"""落地指标（20261001）：判"这一轮/这次改动能不能算过关"的**唯一一处**实现。

## 三件事，三个数，别混

问"达到多少才能落地"时，其实混着三个不同的问题，各要一个不同的数（混成一个数是本仓
反复踩过的坑：一个数被拿去回答三个问题，最后哪个都答不准）：

| 数 | 回答 | 取值 | 置红吗 |
|---|---|---|---|
| `FLOOR` **地板** | 出事故了吗 | **0.78** | **是**——低于它当夜红 |
| `ENTRY` **档位** | 我们爬到哪一级了 | **0.85** → 逐级抬到 `TARGET` | 否，只报 |
| `TARGET` **目标** | 项目要落地到哪 | **0.95** | —— |

**为什么地板不是目标**：夜间那道红必须保持"事故"语义（红了就有人看），而**能力题的
采样噪声本身就很大**。20261001 实测最近 8 次全量：终判红数 6/7/7/8/10/11/11/15 条
（红率 5.2%–11.7%；最差那次 15 红/128 的下界是 **0.8156**），两夜的红名单常常只有两条
重合。把地板设在噪声带里，等于每晚都在
"事故"上叫醒人——红线一响就没人看了（R2 `--keep 3` 那次同族）。

⚠️ **地板的第一个取值（0.82）就是这么错的，改掉的原因值得记住**：它是拿 n=146 的
历史算的"最差一夜 15 红 ⇒ 下界 0.837，再退一档"，可**分母会漂**——用例集加了真写闸、
身份前置、前提闸之后，同一批全量的 n 从 146 掉到 116，同样 15 条红的下界是 0.798。
**「下界」不是分母无关的量**：固定一个下界数，n 越小越容易触发。实测 0.82 在 n=116
上 **≥13 红（11.2%）**就红——正好落在历史噪声带里（最差一夜 11.7%），等于把最坏的一次
正常波动升级成事故。所以取 0.78：三种分母下都要 **≥15% 的红**（n=146 ≥23 条、128 ≥19、
116 ≥17）才响，比历史最差（11.7%）整整高出一档，而真事故（provider 整片挂）红率 ≥50%
——中间隔着一条没人住的沟。**改这个数前先看 `--red-rank` 的当前红率，别对着旧分母调。**

**为什么目标不能当门禁**：27 次全量里够到 0.95 的**只有 2 次**（0 红/112、1 红/128），
都在 20260928 之前；20260928 之后最好的一夜下界 0.8917（6 红/116）。也就是说——**目标
不是"再努一把"，是"回到并且超过 20260928 之前那批全绿/近全绿的夜"**，而用例集在这期间
变严了（真写闸、身份前置、前提闸都在这之后进来）。
把门禁设在 0.95 等于给一个每晚必红的闸。所以 0.95 是**要爬的目标**，爬法是把红**清掉**
（工单见 `--red-rank`），不是把门禁调松。`ENTRY` 是中间的台阶：达档不置红、只记录，
连续 `RAISE_NIGHTS` 夜都达标才抬一档（`raise_hint` 算，`RAISE_STEP` 抬）。

## 两层：硬层不给百分比，采样层才用统计

| 层 | 是什么 | 判据 |
|---|---|---|
| **硬层** | 离线套件 / 真链路探针 / golden 回归组 / 前提与身份前置 | **0 红** |
| **采样层** | 其余能力题（真实 LLM，同一输入不同输出） | **Wilson 95% 下界**（对 `FLOOR` 判红、对 `ENTRY` 记档） |

硬层的红**没有频率含义**：它说的是"这条路存在"。存在 5% 与存在 100% 是同一件事——都得
修，所以给它配百分比等于承认"允许存在一条走通的路"。

**"确定性任务 99%"是个陷阱**（主人 20261001 问过）：回归组 19 条全绿时 Wilson 95% 下界
只有 **0.83**——`.99` 在 n=19 上给不了任何保证，只会让人以为"还剩 1% 余量"。硬层要的
**不是样本量**，而是"每条不变量都有一个**必然触发**它的探针"（写保护有离线锁、令牌作废
有每日真链路探针）。所以硬层判 0，且不为它编一个百分比。

## 改动的落地判据是 A/B，不是绝对值

**绝对值今天到不了 0.95，所以"这次改动能不能上线"不能用绝对值回答。** 本仓对此早有定论
（全量 golden 单跑不可判读，判据只能多遍 A/B）。`ab_compare` 就是那条判据：同一档位下，
改动**前**跑 N 遍、改动**后**跑 N 遍，比两边的下界——**不倒退才落地**（`TOL` 内容差）。
逐条上升的用例逐条点名（聚合没退化 ≠ 没有一条变坏）。

## 优化工单从哪来

`--red-rank` 把全量历史按"**贡献的红次数**"排序打印。20261001 实测：红最集中的 36 条
占了全部红次的 **67%**，而 150 条用例里 **60 条从没红过**——清红要按这张榜从上往下打。

（判据自测见 `tests/test_landing_gate.py`；接线见 `eval/run_golden.py` 末尾的门禁一节。）
"""
import argparse
import glob
import json
import math
import os
import sys

# ── 三个数（唯一取值处；夜间、报告、复审单都从这里读，不在各处重抄）────────────────
FLOOR = 0.78          # 地板：采样层下界低于它 ⇒ 当夜置红（事故闸，不是目标）
ENTRY = 0.85          # 当前档位：达档只记，不置红；连 RAISE_NIGHTS 夜达档 ⇒ 抬档
TARGET = 0.95         # 落地目标（主人定的整体线）
RAISE_STEP = 0.05     # 抬档步长
RAISE_NIGHTS = 3      # 连几夜达档才抬档（单夜达标是运气，连三夜才是水平）
Z95 = 1.96
AB_TOL = 0.005        # A/B：下界回退不超过这个量算"在噪声内"（半个百分点）

RUNS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "report", "runs")
FULL_RUN_MIN_CASES = 100   # 「全量跑」的判据（`--only` 的调试跑不进红榜：分母不可比）


def wilson_ci(passed: int, total: int, z: float = Z95) -> list:
    """通过率的 Wilson 置信区间（20260924 写；20261001 从 `run_golden` 搬到这里）。

    **为什么不是 passed/total 一个数**：110 条里 110 绿，写进报告是「通过率 1.000」——
    读它的人会当成「这个系统不会错」。可 n=110 时「零失败」的 95% 上界仍有约 2.7%
    （rule of three：3/n），换成下界就是真通过率最低可能只有 ~0.966。区间把这句话写进
    数字里，比在文档里补一句"注意样本量"难绕过去。同理，按 tag 分组的那些 n=2、n=3 的
    小组，单看百分比毫无意义——它们**只有**区间有意义。

    取 Wilson 而不是正态近似（Wald）：Wald 在 p 接近 0/1 时会给出越界或零宽区间
    （p=1.0 时宽为 0，正是本仓最常见的形态），Wilson 不会。

    **搬家的理由**：`landing_gate` 要拿它当门禁判据，而 `run_golden` / `golden_full_run`
    / `baseline_group` 又都在用——留两份实现迟早不一致。现在只此一份，
    `run_golden.wilson_ci` 是**再导出**，下游 import 路径一个字不用改。

    返回 `[下界, 上界]`，各四舍五入到 4 位。
    """
    if total <= 0:
        return [0.0, 0.0]
    p = passed / total
    d = 1 + z * z / total
    center = (p + z * z / (2 * total)) / d
    half = z * ((p * (1 - p) / total + z * z / (4 * total * total)) ** 0.5) / d
    return [round(max(0.0, center - half), 4), round(min(1.0, center + half), 4)]


def min_n_zero_fail(target: float, z: float = Z95) -> int:
    """**零失败**时，要声称"下界 ≥ target"所需的最小样本数。

    p=1 时下界退化成 `n/(n+z²)`（见 `wilson_ci`），解 `n ≥ target·z²/(1-target)`：
    target=0.95 ⇒ **73**，0.85 ⇒ 22，0.78 ⇒ 14。

    这个数比"rule of three"给的 60 大——两者问的不是同一件事（那个说的是"失败率上界
    <5%"，这个说的是"通过率下界 >95%"）。写在门禁旁边，是因为**n≈146 看起来很大，让人
    忘了"零失败"这句话本身也要靠样本量撑**：一次 `--only 5 条` 的全绿什么也证明不了。
    """
    return int(math.ceil(target * z * z / (1 - target)))


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
    """这一轮的落地判定（纯函数：喂 `report["cases"]` 与回归组红名单）。

    **采样层的分母是"本轮真跑了的、且不在回归组里的"用例**——回归组由硬层判，两层的红
    不许互相抵消（20260921 分组的分组原意，这里只是让它在判据上真的成立）。被
    `--skip-ids`/`--only` 摘掉的用例**既不在分子也不在分母**（这一轮没被测到），但分母
    因此变小 ⇒ 由 `run_golden.is_full_run` 判"这不是全量"，不在这里重判一次。

    `state` 五种，**只有 `collapse` 置红**：
      · `collapse`    下界 < floor ⇒ **事故**（比历史最差还差）
      · `below_entry` 下界 < entry ⇒ 没到档位，但没塌，只记
      · `pass`        下界 ≥ entry（`raise_ready` 说连几夜了）
      · `at_target`   下界 ≥ target ⇒ 可以谈落地
      · `underpowered` 样本数不够判 entry（n=5 全绿的下界只有 0.51，判红会造出一个永远
                       达不到的门禁，判绿是替样本量撒谎）⇒ **不判、不置红**

    **`underpowered` 有一条前置例外（20261001 当天补）**：小样本也**可能整片塌掉**，而
    "样本不足 ⇒ 不判"会把"1 条全红"放成退出码 0——一个整片失败却绿灯的出口，正是本仓反复
    踩的那族（空分母退 0、点估计把 20 条红读成 0.86 达标）。小样本判塌方用的是**点估计**
    而不是下界：下界在这里没有证明力（n=1 全红的下界恒等于 0，"证据"是空的），点估计才是
    这一轮最诚实的读数（全红 ⇒ 0 < floor ⇒ 塌方）。够不上 floor 的小样本仍走 `underpowered`
    退 0（5 条全绿、21 条 3 红都属此列）——**小样本要么干净、要么出声，就是不假装判过档位**。
    """
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
    return (f"{head}；采样层 {mark} {s['passed']}/{s['total']}"
            f"（点估计 {s['point']:.3f}，下界 {s['lower']:.3f}）{word}；"
            f"档位 {s['entry']} / 目标 {s['target']}（还差 {v['distance_to_target']:.3f}）")


def ab_compare(before: list, after: list, *, tol: float = AB_TOL, z: float = Z95) -> dict:
    """**改动的落地判据**：改动前后各跑 N 遍，比采样层下界（`before`/`after` 是报告列表）。

    **为什么必须是相对判据**：绝对值今天到不了目标（最好一夜 0.906），拿它当"能不能上线"
    会把每一次改动都否掉。而"这次改动有没有把它弄坏"是**可以**回答的——同一档位、同一台机、
    同一批用例，前后各跑几遍，下界不退就是没弄坏。

    **判据只有一条**：`after` 的下界不低于 `before` 的下界（容差 `tol`，半个百分点以内算
    噪声）。逐条上升的用例`per_case_rose` 单独点名——**聚合没退化 ≠ 没有一条变坏**，
    一条 5%→60% 的用例会被另外几条好转抵消掉，那正是最该看的东西。

    样本不足（任一侧 < `min_n_zero_fail(ENTRY)`）⇒ `underpowered`，**不判**：跑 5 条
    去比 A/B 是本仓反复出现过的自欺（区间宽到能装下任何结论）。

    `before`/`after` 可以是报告 dict 列表，也可以是"每组报告里 cases 拼成的一串"——
    传 `[[{case}, {case}...], ...]` 形状时按多次采样合并统计。
    """
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
    """从历史报告里算**每条用例的终判红率**（"按指标优化"的工单）。

    **为什么读历史而不是再跑一遍**：一条用例"是不是慢性红"是**跨轮**性质，单跑不可判读
    （本仓定论）。历史报告全在盘上（852 份，其中 27 次是全量），算它秒级纯 IO。

    只收**全量跑**（`len(cases) >= min_cases`）——`--only` 的调试跑分母不可比，混进来会
    把一条调试时的红算成"历史红率"。

    返回按"红次数"降序；每项含 `runs/red/first_run_red/rate/ci95`。`first_run_red` 与
    `red` 并列是刻意的：两者之差就是"被复跑吸收掉的红斑"，差得越多这条用例的判据越脆
    （见 `run_golden` 头注"放行 ≠ 通过"那条）。
    """
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
    """档位该不该抬：**最近 `nights` 次全量**是否夜夜达档。

    单夜达标是运气（前文那 6–15 条红数摆动就是证据），连三夜才是水平——所以抬档看的是
    "最近 N 夜的**每一夜**都 ≥ 档位"，不是"平均下来够"。历史里没有 `landing` 块的旧报告
    现场按 `verdict` 重算（口径同一处实现，不读第二份）。
    """
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
