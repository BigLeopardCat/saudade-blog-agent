# -*- coding: utf-8 -*-
"""两个跑法的**前提**必须同源：隔离跑法跑用例之前要先把语料索引建好（20261002）。

**病根**：`rag_search.warm_async()` 挂在 `server.py` 的 **lifespan** 上（只有 HTTP app
起来才预热）。隔离跑法（`eval/golden_case_runner.py`，一条用例一个进程）**从不启动那个
app**，父进程也没有别的入口替它预热 ⇒ 子进程跑用例的那一刻索引**恒为空**：
`agent/context.py::_doc_anchors` 把《标题》解析成 id 那一步恒降级（块尾如实注记
「语料索引未就绪」）⇒ planner 拿不到 id、只能去检索，撞上用例的
`forbid_tool_calls: [rag_search …]`。而进程内跑法在用例循环**之前**就调了
`judge_corpus()`（空则同步 build）⇒ **同一个用例，两个跑法结论不同**——这正是本仓
反复治过的那个病（"两个跑法结论必须逐字段对齐"）。

**实测**（20261002 06:40 那轮，隔离跑法）：152 份 trace 里 5 份带那句注记，其中一条是
**回归组**的 `followup_named_doc_title_only_id`（当日唯一的硬层红）。判据侧不做"这个
跑法没接上就放行"的妥协（同 `require_doc_terms` 那条："取不到 ⇒ 判「未评估」"），
所以只能在**跑之前**把前提补齐。

**锁法**：把 `golden_case_runner.main()` 整条驱动起来，`judge_corpus` / `run_case` /
`check_case` 全换桩（不跑模型、不联网、不落报告），断言四件事——
  ① 预热发生在**跑用例之前**（顺序，不是"文件里有这行"）；
  ② 判据复用的是**同一份**快照（跑完再取一次 = 允许语料在两次之间变过）；
  ③ 预热失败（`None`）**不阻断**评测：用例照跑、判据拿到 `None`（那条用例判「未评估」，
     而不是静默通过、也不是整轮中止）；
  ④ 进程内跑法也在循环之前预热（源码顺序锁——两个跑法只有一边接上，等于没接）。

秒级、无网络、无 LLM；由 eval.yml 在 push 时跑。

用法：.venv/bin/python tests/test_golden_runner_parity.py
"""
import contextlib
import importlib.util
import io
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "eval"))

import run_golden as rg  # noqa: E402

FAILS: list[str] = []


def check(desc: str, cond: bool, detail: str = "") -> None:
    print(("  ✅ " if cond else "  ❌ ") + desc + (f"  [{detail}]" if detail else ""))
    if not cond:
        FAILS.append(desc)


def _load_runner():
    """按路径加载 `eval/golden_case_runner.py`。

    刻意**不** `import golden_case_runner`：本文件与它平级、`eval/` 又已在 sys.path 上，
    同名导入会拿缓存里的另一份（改了这个文件、测的却是那个）——本仓"看着有、其实没有"
    的家族里最省事的一种。
    """
    spec = importlib.util.spec_from_file_location(
        "_golden_case_runner_under_test", ROOT / "eval/golden_case_runner.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _stub_result() -> dict:
    """子进程 `main()` 会读的这些键，一个都不能少（少一个 = 报的是 KeyError，
    读起来像"环境坏了"，而不是"那条断言没测着"——同 `golden_rerun_offline_test` 的老教训）。"""
    return {"text": "回答", "commands": [], "tool_calls": [], "rounds": [],
            "exec_rows": [], "exec_tools": [], "tool_rounds": 0, "trace": None,
            "resets": 0, "resets_reasons": [], "reset_scopes": [], "fallback_reasons": [],
            "confirm_tokens": [], "confirm_payloads": [], "error": None}


CASE = {"id": "parity_probe", "tags": ["regression"], "user_input": "问一句",
        "context": {}, "gold": {"nonempty": True}}


def drive(snapshot) -> dict:
    """跑一次子进程 `main()`，记录三个桩的调用顺序与参数。

    `snapshot` 就是预热桩的返回值（`None` = 预热失败那一态）。
    """
    mod = _load_runner()
    tmp = tempfile.mkdtemp(prefix="golden_runner_parity_")
    casefile = os.path.join(tmp, "case.json")
    with open(casefile, "w", encoding="utf-8") as f:
        json.dump(CASE, f, ensure_ascii=False)
    calls: list = []
    saved = (rg.judge_corpus, rg.run_case, rg.check_case, sys.argv)
    try:
        rg.judge_corpus = lambda: (calls.append(("judge_corpus",)), snapshot)[1]

        def _run(case, **kw):
            calls.append(("run_case", case["id"]))
            return _stub_result()

        def _check(case, result, *, docs=None):
            calls.append(("check_case", docs))
            return []

        rg.run_case, rg.check_case = _run, _check
        sys.argv = ["golden_case_runner.py", casefile, os.path.join(tmp, "report")]
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            try:
                mod.main()
                code = 0
            except SystemExit as e:
                code = e.code if isinstance(e.code, int) else 0
        return {"calls": calls, "out": buf.getvalue(), "code": code}
    finally:
        rg.judge_corpus, rg.run_case, rg.check_case, sys.argv = saved
        shutil.rmtree(tmp, ignore_errors=True)


print("① 预热在跑用例**之前**（顺序，不是「文件里有这行」）")
_snap = {"docs": [{"type": "note", "id": 14, "title": "某篇", "content": "正文"}]}
r = drive(_snap)
_order = [c[0] for c in r["calls"]]
check("调用顺序 = 预热 → 跑用例 → 判据",
      _order == ["judge_corpus", "run_case", "check_case"], str(_order))
check("判据复用的是同一份快照（不是跑完再取一次）",
      r["calls"][2][1] is _snap, repr(r["calls"][2][1])[:60])
check("子进程照常收尾、退出码 0（桩判据全过）", r["code"] == 0, str(r["code"]))

print("\n② 预热失败（None）不阻断：用例照跑，判据如实拿到 None")
r2 = drive(None)
check("用例仍然跑了（不是把整轮中止）",
      [c[0] for c in r2["calls"]] == ["judge_corpus", "run_case", "check_case"],
      str([c[0] for c in r2["calls"]]))
check("判据拿到的是 None ⇒ 带 `require_doc_terms` 的用例判「未评估」"
      "（不是静默通过）", r2["calls"][2][1] is None, repr(r2["calls"][2][1]))

print("\n③ 反向对照：把预热挪到跑用例**之后**（= 改回旧形状）⇒ ① 当场红")
_after = [c for c in r["calls"] if c[0] != "judge_corpus"] + [("judge_corpus",)]
check("顺序变了就不再是 [judge_corpus, run_case, check_case]",
      [c[0] for c in _after] != ["judge_corpus", "run_case", "check_case"],
      str([c[0] for c in _after]))

print("\n④ 进程内跑法也在循环之前预热（源码顺序锁：只有一边接上等于没接）")
_src = (ROOT / "eval/run_golden.py").read_text(encoding="utf-8")
_i_warm = _src.find("judge_docs = judge_corpus()")
_i_first_case = _src.find("result = run_case(case, run_id=run_id)")
check("`judge_corpus()` 在第一次 `run_case(...)` 之前",
      _i_warm > 0 and _i_first_case > _i_warm, f"{_i_warm} vs {_i_first_case}")
check("判据吃的是那一份（`docs=judge_docs`）", _src.count("docs=judge_docs") >= 2,
      str(_src.count("docs=judge_docs")))

print()
if FAILS:
    print(f"失败 {len(FAILS)} 项：" + "；".join(FAILS))
    sys.exit(1)
print("全部通过")
