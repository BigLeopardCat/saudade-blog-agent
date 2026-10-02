# -*- coding: utf-8 -*-
"""落地指标判据单测（纯函数 + 合成报告，零网络零 LLM，秒级）。

被测 = `eval/landing_gate.py`：三个数（`FLOOR` 地板 / `ENTRY` 档位 / `TARGET` 目标）、
`verdict` 的五态、`ab_compare` 的落地判据、`chronic_reds` 的工单、`raise_hint` 的抬档，
以及 20261003 的**企业落地三条**（总下界夜夜达档 / 技能面下界 / 用户可见兜底）。

**这份测试真正要锁住的只有一句话**：*三个数各回答一个问题，只有"事故"才置红*。
最容易复发的退化是"把档位当门禁用"——那样 nightly 会在采样噪声上夜夜红，红就不值钱了
（20260929 那条纪律）。所以下面既有正例（below_entry 不置红），也有**反向对照**
（collapse 必须置红；下界真的低于地板时不许被"点估计还行"糊过去）。

另锁两处口径不许分叉：回归组不进采样层分母（硬层由硬层判）、`chronic_reds` 只收全量跑
且按**终判**算（复跑绿的不能算成红）。

用法：.venv/bin/python tests/test_landing_gate.py
"""
import json
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent  # 仓根
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "eval"))

import landing_gate as lg  # noqa: E402

FAILS: list[str] = []


def check(desc: str, cond: bool, detail: str = "") -> None:
    print(("  ✅ " if cond else "  ❌ ") + desc + (f"  [{detail}]" if detail else ""))
    if not cond:
        FAILS.append(desc)


def _cases(n_total: int, n_red: int, *, n_regression: int = 0, prefix: str = "c") -> list:
    """合成一批用例：前 n_regression 条打 regression 标签、最后 n_red 条判红。"""
    out = []
    for i in range(n_total):
        tags = ["regression"] if i < n_regression else ["ability"]
        # 同时写 `ok` 与 `final_ok`（= 报告里"没有复跑"那条路的形状）；只写 final_ok 的话
        # `ok` 缺席会被 `first_run_red` 的 `not c.get("ok")` 读成"首跑红"，红榜就不对了。
        okk = i < n_total - n_red
        out.append({"id": f"{prefix}{i}", "tags": tags, "ok": okk, "final_ok": okk})
    return out


def test_wilson_and_sample_size():
    print("\n① Wilson 区间与样本量规则（判据的算术地基）")
    # 已知值（20261001 从 27 次全量报告里核过）：零失败时下界 = n/(n+z²)
    check("110/110 下界 0.966（rule of three 说的 ~0.966 同一个数）",
          lg.wilson_ci(110, 110)[0] == 0.9663, str(lg.wilson_ci(110, 110)))
    check("146/146 下界 0.974 —— 「全绿」≠「95% 保证了」",
          lg.wilson_ci(146, 146)[0] == 0.9744, str(lg.wilson_ci(146, 146)))
    check("146 里红 2 条 ⇒ 下界 0.951（目标 0.95 在 n=146 上只允许 ≤2 红）",
          lg.wilson_ci(144, 146)[0] == 0.9514, str(lg.wilson_ci(144, 146)))
    check("128 里红 15 条 ⇒ 下界 0.816（实测正常波动里最差的一夜，地板必须低于它）",
          lg.wilson_ci(113, 128)[0] == 0.8156, str(lg.wilson_ci(113, 128)))
    check("反向对照：0.82 会在这条上误报（地板改成 0.78 的直接理由）",
          lg.wilson_ci(113, 128)[0] < 0.82 and lg.wilson_ci(113, 128)[0] > lg.FLOOR)
    check("116 里红 17 条 ⇒ 下界 0.778 < 地板 0.78（当前分母下要 ≥17 红 = 14.7% 才响）",
          lg.wilson_ci(99, 116)[0] == 0.7778 and lg.wilson_ci(99, 116)[0] < lg.FLOOR,
          str(lg.wilson_ci(99, 116)))
    check("零失败要声称 0.95 需 73 条样本（rule of three 的 60 是另一个问题）",
          lg.min_n_zero_fail(0.95) == 73, str(lg.min_n_zero_fail(0.95)))
    check("档位 0.90 需 35 条、地板 0.78 需 14 条",
          (lg.min_n_zero_fail(lg.ENTRY), lg.min_n_zero_fail(lg.FLOOR)) == (35, 14),
          f"{lg.min_n_zero_fail(lg.ENTRY)}/{lg.min_n_zero_fail(lg.FLOOR)}")
    check("空分母给 [0,0] 而不是崩", lg.wilson_ci(0, 0) == [0.0, 0.0])
    # 反向对照：点估计与下界**必须**是两个数，否则"带样本量"就是句空话
    check("反向对照：同一批数据点估计 ≠ 下界（1.000 vs 0.974）",
          lg.wilson_ci(146, 146)[0] < 1.0)


def test_verdict_states():
    print("\n② verdict 五态：只有事故置红，没到档位只记")
    base = dict(floor=lg.FLOOR, entry=lg.ENTRY, target=lg.TARGET)
    v = lg.verdict(_cases(146, 0), [], **base)
    check("全绿 146 ⇒ at_target（下界 0.974 ≥ 0.95）",
          v["sampled"]["state"] == "at_target", v["sampled"]["state"])
    v = lg.verdict(_cases(146, 2), [], **base)
    check("红 2 条 ⇒ 仍 at_target（n=146 上目标只允许 ≤2 红）",
          v["sampled"]["state"] == "at_target", v["sampled"]["state"])
    v = lg.verdict(_cases(146, 6), [], **base)
    check("红 6 条（下界 0.913）⇒ pass：达档，且**不置红**",
          v["sampled"]["state"] == "pass" and v["raise_ready"], v["sampled"]["state"])
    # 抬档的回归锁（20261003：档位 0.85 → 0.90）：同一批数据在旧档位下是 pass，
    # 抬档后落到 below_entry——**不置红**（档位不是门禁），但"达档"这句话不再成立。
    v = lg.verdict(_cases(146, 8), [], **base)
    check("红 8 条（下界 0.896）⇒ below_entry：抬档后不再算达档，但仍**不置红**",
          v["sampled"]["state"] == "below_entry" and not v["raise_ready"],
          v["sampled"]["state"])
    v = lg.verdict(_cases(146, 14), [], **base)
    check("红 14 条（下界 0.846）⇒ below_entry：没到档位，仍**不置红**"
          "（这就是「档位不是门禁」）",
          v["sampled"]["state"] == "below_entry", v["sampled"]["state"])
    v = lg.verdict(_cases(146, 23), [], **base)
    check("反向对照：红 23 条（下界 0.775）⇒ collapse —— 低于地板**必须**置红",
          v["sampled"]["state"] == "collapse"
          and v["sampled"]["collapse_basis"] == "lower", v["sampled"]["state"])
    check("实测最差的那次**事故**（25 红/116 = 21.6%）在新地板上照样置红",
          lg.verdict(_cases(116, 25), [], **base)["sampled"]["state"] == "collapse")
    check("而实测最差的**正常**一夜（15 红/128）不置红——地板就该落在这条缝里",
          lg.verdict(_cases(128, 15), [], **base)["sampled"]["state"] == "below_entry")
    v = lg.verdict(_cases(5, 0), [], **base)
    check("5 条全绿 ⇒ underpowered：不判、不置红（判它是替样本量撒谎）",
          v["sampled"]["state"] == "underpowered" and not v["raise_ready"],
          v["sampled"]["state"])
    check("underpowered 的门槛是 min_n_zero_fail(档位) = 35，不是拍脑袋",
          lg.verdict(_cases(34, 0), [], **base)["sampled"]["state"] == "underpowered"
          and lg.verdict(_cases(35, 0), [], **base)["sampled"]["state"] == "pass")
    # 小样本**整片塌方**必须置红（20261001 当天补）：1 条全红若走 underpowered 就是退 0
    # ——"整片失败却绿灯"正是本仓反复踩的那族（空分母退 0、点估计把红读成达标）。
    v = lg.verdict(_cases(1, 1), [], **base)
    check("反向对照：1 条全红 ⇒ collapse（点估计 0 < 地板；下界 0 在这里什么也证明不了）",
          v["sampled"]["state"] == "collapse" and v["sampled"]["collapse_basis"] == "point",
          f"{v['sampled']['state']}/{v['sampled']['collapse_basis']}")
    check("小样本塌方看**点估计**（10 条全红 ⇒ 0.0；5 条 4 红 ⇒ 0.2 也低于地板）",
          lg.verdict(_cases(10, 10), [], **base)["sampled"]["state"] == "collapse"
          and lg.verdict(_cases(5, 4), [], **base)["sampled"]["state"] == "collapse")
    check("点估计够得上地板就仍不判（5 条 1 红 ⇒ 0.8 ≥ 0.78 ⇒ 样本量问题，不是塌方）",
          lg.verdict(_cases(5, 1), [], **base)["sampled"]["state"] == "underpowered"
          and lg.verdict(_cases(5, 0), [], **base)["sampled"]["collapse_basis"] == "")
    check("空分母不按塌方判（0 条是「没测到」，由 run_golden 的退出码 2 管）",
          lg.verdict([], [], **base)["sampled"]["state"] == "underpowered")
    # 硬层与采样层各判各的：回归组红不影响采样层读数（也不被它抵消）
    v = lg.verdict(_cases(146, 8, n_regression=19), ["c0"], **base)
    # （判的是"两层各算各的"：硬层红不改变采样层的分母与读法。它这一格读数是
    # below_entry 还是 pass 是数据的事，与这条断言无关——所以只锁分母与"有判决"。）
    check("回归组红 ⇒ 硬层 ok=False，但采样层照样按能力题算",
          (not v["hard"]["ok"]) and v["sampled"]["total"] == 127
          and v["sampled"]["state"] == "below_entry", str(v["sampled"]["total"]))
    check("硬层判据写的是「0 红」而不是某个百分比",
          "0 红" in v["hard"]["criterion"] and "%" not in v["hard"]["criterion"])


def test_ab_compare():
    print("\n③ A/B：改动的落地判据（绝对值今天到不了目标，只能用相对判据）")
    def rep(cases: list) -> dict:
        return {"cases": cases}
    before = [rep(_cases(60, 6, prefix="a")), rep(_cases(60, 6, prefix="a"))]
    after = [rep(_cases(60, 6, prefix="a")), rep(_cases(60, 6, prefix="a"))]
    r = lg.ab_compare(before, after)
    check("前后同分布 ⇒ no_regression",
          r["verdict"] == "no_regression" and abs(r["delta_lower"]) < 1e-9, r["note"])
    after_worse = [rep(_cases(60, 30, prefix="a")), rep(_cases(60, 30, prefix="a"))]
    r = lg.ab_compare(before, after_worse)
    check("反向对照：改动后红翻五倍 ⇒ regressed",
          r["verdict"] == "regressed" and r["delta_lower"] < 0, r["note"])
    r = lg.ab_compare([rep(_cases(10, 0, prefix="a"))], [rep(_cases(10, 0, prefix="a"))])
    check("样本不足（10 条）⇒ underpowered，**不判**（跑 5 条去比 A/B 是自欺）",
          r["verdict"] == "underpowered", r["note"])
    # 这条是 A/B 判据里最要紧的一条：聚合没退化 ≠ 没有一条变坏
    b = _cases(60, 0, prefix="w")
    for i in range(3):                      # 改动前：w0..w2 红，每条 2/2
        b[i]["ok"] = b[i]["final_ok"] = False
    a = _cases(60, 0, prefix="w")
    for i in range(3, 6):                   # 改动后：换成 w3..w5 红，红数一样（6/120）
        a[i]["ok"] = a[i]["final_ok"] = False
    r = lg.ab_compare([rep(b), rep(b)], [rep(a), rep(a)])
    check("逐条对比能点名「聚合把它平均掉」的上升项（红数一样也必须点名）",
          r["verdict"] == "no_regression" and bool(r["per_case_rose"]),
          f"verdict={r['verdict']} rose={[e['id'] for e in r['per_case_rose'][:3]]}")
    check("…同时点名下降的那三条（只报上升会让人以为改动只做了坏事）",
          [e["id"] for e in r["per_case_fell"]] == ["w0", "w1", "w2"],
          str([e["id"] for e in r["per_case_fell"]]))
    check("只报两边都跑过的用例（cases_compared 有数）",
          r["cases_compared"] == 60, str(r["cases_compared"]))


def test_chronic_reds(tmp: Path):
    print("\n④ 慢性红榜：只收全量跑、按终判算（这两条错了整张榜就是错的）")
    runs = tmp / "runs"
    runs.mkdir()
    def write(name: str, cases: list) -> None:
        (runs / name).write_text(json.dumps({"cases": cases}), encoding="utf-8")
    full_a = _cases(120, 0, prefix="p")
    full_a[3]["final_ok"] = True            # 首跑红、复跑绿 ⇒ 终判**不算红**
    full_a[3]["ok"] = False
    full_a[3]["rerun"] = {"ok": True}
    full_a[7]["final_ok"] = False           # 真红
    full_a[7]["ok"] = False
    write("20261001_010101.json", full_a)
    full_b = _cases(120, 0, prefix="p")
    full_b[7]["final_ok"] = False
    write("20261002_010101.json", full_b)
    write("20261003_010101.json", _cases(4, 4, prefix="p"))   # 调试跑 ⇒ 不进榜
    rows = lg.chronic_reds(str(runs))
    check("只有 p7 上榜（p3 是复跑绿的、调试跑被整份排除）",
          [r["id"] for r in rows] == ["p7"], str([r["id"] for r in rows]))
    check("红 2/2、首跑红 1/2 —— 两个数并列，差额就是被复跑吸收掉的那次",
          (rows[0]["red"], rows[0]["runs"], rows[0]["first_run_red"]) == (2, 2, 1),
          str(rows[0]))
    check("chronic_map 给复审单用的 (红, 次数) 形状",
          lg.chronic_map(str(runs)) == {"p7": (2, 2)})
    check("min_runs 能滤掉只出现过一次的",
          lg.chronic_reds(str(runs), min_runs=3) == [], "空")


def test_raise_hint(tmp: Path):
    print("\n⑤ 抬档提示：要「夜夜达档」这个证据，不是一夜运气")
    runs = tmp / "runs2"
    runs.mkdir()
    def write(name: str, n_red: int) -> None:
        (runs / name).write_text(
            json.dumps({"cases": _cases(146, n_red, prefix="q")}), encoding="utf-8")
    for d in ("01", "02"):
        write(f"202610{d}_010101.json", 6)      # 下界 0.913 ≥ 0.90
    h = lg.raise_hint(str(runs))
    check("只有 2 次全量 ⇒ 不够 3 夜，不提示（单夜/两夜达标是运气）",
          not h["ready"] and "不够" in h["reason"], h["reason"])
    write("20261003_010101.json", 20)           # 第三夜塌了（下界 0.798）
    h = lg.raise_hint(str(runs))
    check("反向对照：三夜里有一夜没达档 ⇒ 不抬",
          not h["ready"] and len(h["lows"]) == 3, h["reason"])
    write("20261004_010101.json", 6)
    write("20261005_010101.json", 6)
    h = lg.raise_hint(str(runs))
    check("最近三夜（03 塌、04/05 达标）⇒ 仍不抬 —— 只看最近 N 夜，不看平均",
          not h["ready"], h["reason"])
    write("20261006_010101.json", 6)
    h = lg.raise_hint(str(runs))
    check("再一夜达标 ⇒ 最近三夜（04/05/06）夜夜达档 ⇒ 提示抬到 0.95（下一档是 TARGET）",
          h["ready"] and h["next"] == 0.95, h["reason"])


def test_wiring():
    print("\n⑥ 接线锁：判据只有一处实现，门禁只在事故时红")
    src = (ROOT / "eval" / "run_golden.py").read_text(encoding="utf-8")
    check("run_golden 用 landing_gate.verdict（不在本文件里重算阈值/区间）",
          "landing_gate.verdict(results, _reg_bad)" in src)
    check("判定落进报告（报告里能读到两层结论）", '"landing": _landing,' in src)
    check("**只有 collapse 退出码 1**；below_entry 不置红（档位不是门禁）",
          'if _s["state"] == "collapse":' in src
          and '_s["state"] == "below_entry":' not in src)
    check("Wilson 只有一份实现：run_golden 从 landing_gate 再导出",
          "from landing_gate import wilson_ci" in src
          and "def wilson_ci" not in src)
    check("复审单给每条红印历史红率（归类从猜变成读）",
          "landing_gate.chronic_map()" in src and "历史（全量跑的终判口径）" in src)
    nightly = (ROOT / "scripts" / "nightly_regression.sh").read_text(encoding="utf-8")
    run_lines = [ln for ln in nightly.splitlines()
                 if "eval/run_golden.py" in ln and not ln.strip().startswith("#")]
    check("夜间**不传** --min-pass-rate（传了就退回点估计口径）",
          bool(run_lines) and all("--min-pass-rate" not in ln for ln in run_lines),
          "；".join(ln.strip() for ln in run_lines))
    check("地板的来处写在模块里（注释能追到实测最差那夜的 0.8156 与分母漂移）",
          "0.8156" in (ROOT / "eval" / "landing_gate.py").read_text(encoding="utf-8")
          and "分母" in (ROOT / "eval" / "landing_gate.py").read_text(encoding="utf-8"))


def _tagged(cid: str, tags: list, ok: bool = True, **kw) -> dict:
    """一条带任意 tag 的合成用例（`_cases` 只给 regression/ability 两类，不够用）。"""
    return {"id": cid, "tags": list(tags), "ok": ok, "final_ok": ok, **kw}


def _night(runs: Path, name: str, cases: list) -> None:
    (runs / name).write_text(json.dumps({"cases": cases}), encoding="utf-8")


def test_enterprise_criteria(tmp: Path):
    print("\n⑦ 企业落地三条：面判据跨夜聚合、只有 scope=text 才算伤害")
    # ── 伤害口径：**只有终局兜底（scope=text）**算"用户真收到道歉" ──
    # scope=all 是 gate 打回后 planner 重规划答对了 —— 用户看到的是真回答，
    # 拿 `resets` 计数当判据会把"打回"报成"道歉"，两者对体验的意思正好相反。
    # 合成形状照**报告里的真形状**：`fallback_reasons` 只装 scope=text 那几条
    # （run_golden 的解析处就在 text 分支里 append），`reset_scopes` 装全部。
    harm_cases = [
        _tagged("h0", ["ability"]),
        _tagged("h1", ["ability"], reset_scopes=["text"], resets=1,
                fallback_reasons=["叙述校验未通过，已替换为如实回复"]),
        _tagged("h2", ["ability"], reset_scopes=["all"], resets=1, fallback_reasons=[]),
        _tagged("h3", ["ability"], reset_scopes=["all", "text"], resets=2,
                fallback_reasons=["兜底"]),
    ]
    h = lg.harm_stats(harm_cases)
    check("只有 scope=text 的轮次算伤害（scope=all 独自出现不算）",
          h["ids"] == ["h1", "h3"] and h["hit"] == 2 and h["rounds"] == 4, str(h["ids"]))
    check("反向对照：按 `resets` 计数会把 h2 也算进来（3 轮 vs 2 轮）——"
          "「打回后重规划答对了」被报成「用户收到了道歉」正是拆 scope 要拦的误判",
          len([c for c in harm_cases if c.get("resets")]) == 3 and h["hit"] == 2)
    check("reset_scopes 缺席 ⇒ 退回旧口径、保守算伤害（键缺了是「不知道是哪种」）",
          lg.harm_stats([_tagged("x", ["ability"], resets=2)])["ids"] == ["x"])
    check("`fallback_reasons` 只有计数没有逐条理由时按 scope 补位（形状对得上一半）",
          lg.harm_stats([_tagged("y", ["ability"], reset_scopes=["all", "text"])])["ids"] == ["y"])
    check("三态按占比分：<1% 绿、≥1% 黄、≥5% 事故（0.05 是「gate 整片误伤」线）",
          [lg.harm_stats([_tagged(f"z{i}", ["ability"]) for i in range(100)]
                         + [_tagged(f"b{i}", ["ability"], reset_scopes=["text"],
                                    fallback_reasons=["兜底"]) for i in range(k)])["state"]
           for k in (0, 2, 6)] == ["ok", "hint", "incident"],
          str([lg.harm_stats([_tagged(f"z{i}", ["ability"]) for i in range(100)]
                             + [_tagged(f"b{i}", ["ability"], reset_scopes=["text"],
                                        fallback_reasons=["兜底"]) for i in range(k)])["rate"]
               for k in (0, 2, 6)]))
    check("判据文案把两条线都写出来（读数与阈值同一处，不在打印里手抄）",
          "0.01" in h["criterion"] and "0.05" in h["criterion"], h["criterion"])

    # ── 面聚合：只收非回归组、一条用例进它挂的每个面、小面不判 ──
    ds = lg.domain_rates([
        _tagged("a", ["multi_step", "20260927"], ok=False),
        _tagged("b", ["multi_step"]),
        _tagged("c", ["multi_step"], ok=False),
        _tagged("d", ["regression"], ok=False),        # 硬层不进口径（与采样层同总体）
        _tagged("e", ["capability"]),
    ], min_rounds=3)
    by = {r["tag"]: r for r in ds}
    check("回归组不进面（面读数与总读数必须同总体）",
          "regression" not in by)
    check("一条用例进它挂的每个面（多步链同时算 multi_step 与 20260927）",
          by["multi_step"]["rounds"] == 3 and by["20260927"]["rounds"] == 1,
          f'multi_step={by["multi_step"]["rounds"]} 20260927={by["20260927"]["rounds"]}')
    check("面自己算下界（3 条 2 红 ⇒ 点估计 0.333，下界 0.062 比它更低）",
          by["multi_step"]["point"] == 0.3333 and by["multi_step"]["lower"] < 0.3333,
          str(by["multi_step"]["ci95"]))
    check("轮次不够的面 `judged=False`（不判、也不算短板：小面要么干净要么出声）",
          by["20260927"]["judged"] is False and by["capability"]["judged"] is False
          and by["multi_step"]["judged"] is True)
    check("按**下界升序**排（最弱的排前面 = 工单顺序）",
          [r["lower"] for r in ds] == sorted(r["lower"] for r in ds))

    # ── readiness：三条一起判，且面判据要**跨夜聚合**（单夜每面只有几条，没有证明力）──
    def fixture(runs: Path, *, red_in_face: int = 0, face_rounds: int = 8,
                hint_face: bool = False) -> None:
        """5 夜，每夜 200 条：`face_rounds` 条挂 multi_step（红 `red_in_face` 条），
        另 8 条挂 knowledge（`hint_face` 时首夜红 1 条），其余挂 core 全绿。"""
        runs.mkdir()
        for d in range(1, 6):
            cases = [_tagged(f"core{d}_{i}", ["core"]) for i in range(200 - face_rounds - 8)]
            cases += [_tagged(f"ms{d}_{i}", ["multi_step"], ok=(i >= red_in_face))
                      for i in range(face_rounds)]
            cases += [_tagged(f"kn{d}_{i}", ["knowledge"],
                              ok=not (hint_face and d == 1 and i == 0)) for i in range(8)]
            _night(runs, f"2026100{d}_010101.json", cases)

    d1 = tmp / "ready_ok"
    fixture(d1)
    r = lg.readiness(str(d1), nights=5, min_cases=1)
    check("5 夜零红 ⇒ ready：总下界夜夜达档 + 无面低于地板 + 无兜底伤害",
          r["ready"] is True, str([c["key"] for c in r["criteria"] if not c["ok"]]))
    check("…而且 strong 也成立：每夜 200 条全绿的下界 0.981 ≥ 目标 0.95 "
          "（strong 是**每夜**都要够，不是平均够）",
          r["strong"] is True and r["cases"] == 1000 and len(r["lows"]) == 5, str(r["lows"][:2]))
    check("面是**跨夜聚合**的（5 夜 × 8 条 = 40 轮，不是每夜 8 轮各判一次）",
          {x["tag"]: x["rounds"] for x in r["domains"]}
          == {"multi_step": 40, "knowledge": 40, "core": 920},
          str({x["tag"]: x["rounds"] for x in r["domains"]}))

    d2 = tmp / "ready_weak"
    fixture(d2, red_in_face=3, hint_face=True)
    r2 = lg.readiness(str(d2), nights=5, min_cases=1)
    check("反向对照：multi_step 面 25/40（下界 0.47 < 地板）⇒ ready=False，并点名在 weak_domains",
          r2["ready"] is False and [w["tag"] for w in r2["weak_domains"]] == ["multi_step"],
          f'ready={r2["ready"]} weak={[(w["tag"], w["lower"]) for w in r2["weak_domains"]]}')
    check("总下界那一条仍 ok（4 红/200 ⇒ 下界 0.95 ≥ 档位）—— 面判据抓的正是它看不见的东西",
          [c["ok"] for c in r2["criteria"] if c["key"] == "overall"] == [True],
          str(r2["lows"][:2]))
    check("0.78–0.90 之间的面进 hint_domains（知识面 39/40 = 0.871）而不是 weak",
          [x["tag"] for x in r2["hint_domains"]] == ["knowledge"],
          str([(x["tag"], x["lower"]) for x in r2["hint_domains"]]))
    check("面判据**不置红**：它只让 ready/strong 变假，退出码仍由 FLOOR/HARM 决定",
          r2["ready"] is False and "置红" not in
          [c["detail"] for c in r2["criteria"] if c["key"] == "domains"][0])

    d3 = tmp / "ready_harm"
    fixture(d3)
    for p in sorted(d3.glob("*.json")):
        rep = json.loads(p.read_text(encoding="utf-8"))
        for c in rep["cases"][:12]:                      # 12/200 = 6% ≥ 事故线 5%
            c["reset_scopes"] = ["text"]
            c["fallback_reasons"] = ["叙述校验未通过"]
            c["final_ok"] = c["ok"] = False
        p.write_text(json.dumps(rep), encoding="utf-8")
    r3 = lg.readiness(str(d3), nights=5, min_cases=1)
    check("用户可见兜底 6% ≥ 事故线 ⇒ ready=False（这一条是**用户真的受到的伤害**）",
          r3["ready"] is False and r3["harm"]["state"] == "incident", r3["harm"]["criterion"])

    r4 = lg.readiness(str(d1), nights=9, min_cases=1)
    check("全量夜数不够时 total 那条不判 ok（「没评」与「通过」必须分开）",
          r4["ready"] is False and "不够" in
          [c["detail"] for c in r4["criteria"] if c["key"] == "overall"][0])


def test_wiring():
    print("\n⑥ 接线锁：判据只有一处实现，门禁只在事故时红")
    src = (ROOT / "eval" / "run_golden.py").read_text(encoding="utf-8")
    check("run_golden 用 landing_gate.verdict（不在本文件里重算阈值/区间）",
          "landing_gate.verdict(results, _reg_bad)" in src)
    check("判定落进报告（报告里能读到两层结论）", '"landing": _landing,' in src)
    check("**只有 collapse 退出码 1**；below_entry 不置红（档位不是门禁）",
          'if _s["state"] == "collapse":' in src
          and '_s["state"] == "below_entry":' not in src)
    check("Wilson 只有一份实现：run_golden 从 landing_gate 再导出",
          "from landing_gate import wilson_ci" in src
          and "def wilson_ci" not in src)
    check("`fallback_resets` 同样只有一份实现（伤害判据的口径不许分叉）",
          "from landing_gate import fallback_resets" in src
          and "def fallback_resets" not in src
          and "def fallback_resets" in (ROOT / "eval" / "landing_gate.py").read_text(
              encoding="utf-8"))
    check("用户可见兜底事故在夜里能当事故闸（退出码 1），且判的是 scope=text 那条路",
          '_h["state"] == "incident"' in src and '_landing["harm"]' in src)
    check("报告里 `fallback_reasons` **只装 text 那几条**（伤害口径的上游形状，"
          "错在这里则 harm_stats 读到的就是「所有打回」）",
          "fallback_reasons.append(_reason)" in src
          and src.index("fallback_reasons.append(_reason)") > src.index('if _scope != "text":'))
    check("复审单给每条红印历史红率（归类从猜变成读）",
          "landing_gate.chronic_map()" in src and "历史（全量跑的终判口径）" in src)
    nightly = (ROOT / "scripts" / "nightly_regression.sh").read_text(encoding="utf-8")
    run_lines = [ln for ln in nightly.splitlines()
                 if "eval/run_golden.py" in ln and not ln.strip().startswith("#")]
    check("夜间**不传** --min-pass-rate（传了就退回点估计口径）",
          bool(run_lines) and all("--min-pass-rate" not in ln for ln in run_lines),
          "；".join(ln.strip() for ln in run_lines))
    check("地板的来处写在模块里（注释能追到实测最差那夜的 0.8156 与分母漂移）",
          "0.8156" in (ROOT / "eval" / "landing_gate.py").read_text(encoding="utf-8")
          and "分母" in (ROOT / "eval" / "landing_gate.py").read_text(encoding="utf-8"))


def main():
    tmp = Path(tempfile.mkdtemp(prefix="landing_gate_test_"))
    try:
        test_wilson_and_sample_size()
        test_verdict_states()
        test_ab_compare()
        test_chronic_reds(tmp)
        test_raise_hint(tmp)
        test_enterprise_criteria(tmp)
        test_wiring()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)   # 本仓纪律：mkdtemp 必须配 rmtree
    if FAILS:
        print(f"\n=== {len(FAILS)} 项失败 ===")
        for f in FAILS:
            print("  -", f)
        sys.exit(1)
    print("\n=== 全部通过 ===")


if __name__ == "__main__":
    main()
