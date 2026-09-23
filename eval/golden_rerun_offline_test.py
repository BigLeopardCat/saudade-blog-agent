# -*- coding: utf-8 -*-
"""回归组「FAIL 重跑一次再判」的离线锁（20260924）。

被锁的纪律：回归组（`tags` 含 regression）是 L2 里唯一"一条红即整轮红"的硬判据，而它
红的原因里混着方差（判据脆弱 / 采样波动）——对**首跑红的回归用例各重跑一次**：复跑仍红
= 真 FAIL；复跑绿 = 按方差放行，但首跑红与复跑绿两条都必须出现在报告/打印/复审单里
（放行 ≠ 静默宽恕）。

做法：把 `eval/run_golden.py` 的 `main()` **整条驱动起来**，只把两处换成桩——`run_one`
（真 LLM 那一跳）与 `ensure_agent`（建图）——再按用例逐轮喂"首跑/复跑"结论，断言退出码、
报告字段、复审单、打印与 `run_one` 实际收到的参数。**判据与控制流是真的，只有 LLM 是假的**：
本文件要抓的正是接线级缺陷（复跑到绿为止 / 复跑覆盖首跑的结论与 trace / 放行了却不出声 /
能力题被顺手重跑），这些靠跑一次真实全量（~20min 且带随机性）都看不出来。

隔离：chdir 到临时目录——run_golden 里 `eval/golden/basic.jsonl`、`eval/report/**` 全是
**相对路径**，整体落进 tmpdir；外加 `GOLDEN_NO_TRACE=1`（不落 golden trace）⇒ 生产报告、
留档、trace **零写入**。秒级、零网络、零 LLM。
"""
import contextlib
import glob
import io
import json
import os
import shutil
import sys
import tempfile
import types
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))

import golden_trace          # noqa: E402
import run_golden as rg      # noqa: E402

FAILS: list[str] = []


def check(desc: str, cond: bool, detail: str = "") -> None:
    print(("  ✅ " if cond else "  ❌ ") + desc + (f"  [{detail}]" if detail else ""))
    if not cond:
        FAILS.append(desc)


def mkcase(cid: str, tags: list[str]) -> dict:
    """最小用例：只有一条 text_contains 正断言（桩按绿/红给出两种文本）。"""
    return {"id": cid, "tags": tags, "user_input": f"问题-{cid}", "context": {},
            "gold": {"text_contains": ["回答"]}}


def _stub_run_one(plan: dict, calls: list):
    """桩：按 `plan[case_id]` 顺次弹出这一跑判绿(True)/判红(False)，并记录收到的参数。"""

    def _run(req, principal=None, trace_ctx=None):
        cid = (trace_ctx or {}).get("case", "")
        base = cid[: -len("__rerun")] if cid.endswith("__rerun") else cid
        calls.append({"case": cid, "message": req.message,
                      "principal": (principal.uid, principal.role) if principal else None})
        seq = plan.setdefault(base, [])
        ok = seq.pop(0) if seq else False
        return {"text": "回答" if ok else "跑偏", "commands": [], "tool_calls": [],
                "frames": [], "exec_rows": [], "exec_tools": [], "tool_rounds": 0,
                "trace": None, "resets": 0 if ok else 1,
                "resets_reasons": [] if ok else ["stub 判红"], "error": None}

    return _run


def drive(cases: list[dict], plan: dict, argv_extra: list[str] | None = None) -> dict:
    """在临时目录里整条跑一次 `main()`，收齐退出码/报告/打印/复审单/run_one 调用序列。"""
    tmp = tempfile.mkdtemp(prefix="golden_rerun_test_")
    os.makedirs(os.path.join(tmp, "eval", "golden"))
    with open(os.path.join(tmp, "eval", "golden", "basic.jsonl"), "w", encoding="utf-8") as f:
        for c in cases:
            f.write(json.dumps(c, ensure_ascii=False) + "\n")

    calls: list = []
    saved = (rg.run_one, rg.ensure_agent, sys.argv)
    had_corpus = sys.modules.get("corpus_check")
    fake = types.ModuleType("corpus_check")
    fake.presence_check = lambda: {}    # 语料在位性检查读生产语料：这里是纯控制流测试
    sys.modules["corpus_check"] = fake
    os.environ[golden_trace.ENV_OFF] = "1"   # 不落 golden trace（生产 trace 目录零写入）
    old_cwd = os.getcwd()
    try:
        os.chdir(tmp)
        rg.run_one = _stub_run_one(plan, calls)
        rg.ensure_agent = lambda: None
        sys.argv = ["run_golden.py"] + list(argv_extra or [])
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            try:
                rg.main()
                code = 0
            except SystemExit as e:
                code = e.code if isinstance(e.code, int) else 0
        report = json.load(open(os.path.join(tmp, "eval", "report", "last_run.json"),
                               encoding="utf-8"))
        archived = sorted(glob.glob(os.path.join(tmp, "eval", "report", "runs", "*.json")))
        reviews = sorted(glob.glob(os.path.join(tmp, "eval", "report", "review_*.md")))
        # 留档内容当场读出来：tmpdir 在本函数返回前就被清了（返回路径等于返回死链）
        return {"code": code, "report": report, "out": buf.getvalue(),
                "archived": archived,
                "archived_doc": (json.load(open(archived[0], encoding="utf-8"))
                                 if archived else None),
                "review": open(reviews[0], encoding="utf-8").read() if reviews else "",
                "calls": calls}
    finally:
        os.chdir(old_cwd)
        rg.run_one, rg.ensure_agent, sys.argv = saved
        if had_corpus is None:
            sys.modules.pop("corpus_check", None)
        else:
            sys.modules["corpus_check"] = had_corpus
        shutil.rmtree(tmp, ignore_errors=True)


RED, GREEN = False, True

print("① 首跑红、复跑仍红 → 真 FAIL（照旧硬判，退出码 1）")
r = drive([mkcase("reg_red_both", ["regression", "hallucination"])],
          {"reg_red_both": [RED, RED]})
rep = r["report"]
check("退出码 1（回归组硬判，不按通过率放行）", r["code"] == 1, str(r["code"]))
check("复跑仍红进 regression.failed_ids", rep["regression"]["failed_ids"] == ["reg_red_both"]
      and rep["regression"]["all_passed"] is False, str(rep["regression"]))
check("flaked_ids 为空（复跑没绿就不许当方差）", rep["regression"]["flaked_ids"] == [])
check("恰好多跑一次（**不许重跑到绿为止**）",
      [c["case"] for c in r["calls"]] == ["reg_red_both", "reg_red_both__rerun"],
      str([c["case"] for c in r["calls"]]))
check("复跑与首跑是**同一用例、同一身份**（不是拿别的用例凑出来的绿）",
      r["calls"][0]["message"] == r["calls"][1]["message"]
      and r["calls"][0]["principal"] == r["calls"][1]["principal"])
check("首跑与复跑的结论各存一份（cases[].ok / cases[].rerun.ok 都在）",
      rep["cases"][0]["ok"] is False and rep["cases"][0]["rerun"]["ok"] is False
      and rep["cases"][0]["final_ok"] is False)
check("首跑输出没被复跑覆盖（报告里两段文本分开存）",
      rep["cases"][0]["text"] == "跑偏" and rep["cases"][0]["rerun"]["text"] == "跑偏")
check("复审单置顶「不得放行」且标明复跑仍红",
      "不得放行" in r["review"] and "复跑：仍红" in r["review"], r["review"][:80])

print("\n② 首跑红、复跑绿 → 按方差放行，但必须响")
r = drive([mkcase("reg_flake", ["regression"]), mkcase("reg_green", ["regression"])],
          {"reg_flake": [RED, GREEN], "reg_green": [GREEN]})
rep = r["report"]
check("退出码 0（终判按复跑）", r["code"] == 0, str(r["code"]))
check("首跑红的用例进了 regression.flaked_ids", rep["regression"]["flaked_ids"] == ["reg_flake"]
      and rep["regression"]["failed_ids"] == [] and rep["regression"]["all_passed"] is True,
      str(rep["regression"]))
check("报告同时留着首跑口径（failed_first_run=1 / failed=0）",
      rep["failed_first_run"] == 1 and rep["failed"] == 0)
check("cases[] 里两条结论都在（首跑红 / 复跑绿）",
      rep["cases"][0]["ok"] is False and rep["cases"][0]["rerun"]["ok"] is True
      and rep["cases"][0]["final_ok"] is True)
check("汇总「复跑绿」出声（放行不等于静默宽恕）",
      "复跑才绿" in r["out"] and "按方差放行" in r["out"], r["out"][-200:])
check("首跑绿的不重跑（calls 只有一条 reg_green）",
      [c["case"] for c in r["calls"]] == ["reg_flake", "reg_green", "reg_flake__rerun"],
      str([c["case"] for c in r["calls"]]))
check("复审单为「只有复跑才绿」也生成，并置顶点名该用例",
      "复跑才绿" in r["review"] and "reg_flake" in r["review"]
      and "复跑：绿（按方差放行）" in r["review"], r["review"][:120])
check("留档也写了一份（runs/<ts>.json，与 last_run.json 同口径）",
      len(r["archived"]) == 1
      and r["archived_doc"]["regression"]["flaked_ids"] == ["reg_flake"]
      and r["archived_doc"]["failed_first_run"] == 1)

print("\n③ 能力题首跑红 → 不重跑（本来就按通过率放宽，不占第二条判据）")
r = drive([mkcase("abil_red", ["chat"])], {"abil_red": [RED]})
rep = r["report"]
check("退出码 1（通过率 0 < 门禁 1.0）", r["code"] == 1, str(r["code"]))
check("一次都不重跑", [c["case"] for c in r["calls"]] == ["abil_red"],
      str([c["case"] for c in r["calls"]]))
check("回归组块分母为 0（不把能力题算进回归组）",
      rep["regression"]["total"] == 0 and rep["regression"]["failed_ids"] == [])
check("cases[].rerun 显式为 None（没有的事不许含糊）",
      rep["cases"][0]["rerun"] is None and rep["cases"][0]["final_ok"] is False)

print("\n④ 全绿：不动、不吭声、不留复审单")
r = drive([mkcase("reg_ok", ["regression"]), mkcase("abil_ok", ["chat"])],
          {"reg_ok": [GREEN], "abil_ok": [GREEN]})
check("退出码 0", r["code"] == 0, str(r["code"]))
check("没有复跑", [c["case"] for c in r["calls"]] == ["reg_ok", "abil_ok"])
check("不写复审单", r["review"] == "")
check("打印里不出现「复跑才绿」", "复跑才绿" not in r["out"])

print("\n⑤ --only 挑一条回归用例时同样走复跑（诊断链路时口径一致）")
r = drive([mkcase("reg_a", ["regression"]), mkcase("reg_b", ["regression"])],
          {"reg_a": [RED, GREEN]}, argv_extra=["--only", "reg_a"])
check("--only 之外的用例不跑、也不进报告分母",
      [c["case"] for c in r["calls"]] == ["reg_a", "reg_a__rerun"]
      and r["report"]["total"] == 1, str([c["case"] for c in r["calls"]]))
check("复跑绿照样按方差放行", r["code"] == 0
      and r["report"]["regression"]["flaked_ids"] == ["reg_a"])

print("\n" + ("全部通过" if not FAILS else f"失败 {len(FAILS)} 项：" + "; ".join(FAILS)))
raise SystemExit(1 if FAILS else 0)
