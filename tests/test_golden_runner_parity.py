# -*- coding: utf-8 -*-
"""**判据的输入不许随跑法变**：同一个用例，进程内跑法与隔离跑法必须得到同一个答案（20261002）。

两个跑法各有驱动（`eval/run_golden.py::main` 进程内；`eval/golden_full_run.py` 父进程 +
`eval/golden_case_runner.py` 子进程），共享同一套判据。共享判据只在**输入也共享**时才成立，
而这一天连着抓到两处"输入在两个跑法里不是同一个东西"：

  ① **语料索引**：进程内跑法在用例循环**之前**预热（`judge_corpus()`），隔离子进程从不预热
     （`warm_async()` 挂在 `server.py` 的 lifespan 上，子进程不启动那个 app）⇒ 文档锚点降级
     （块尾注记「语料索引未就绪」）；本文件 ①②③④ 锁它。
  ② **`forbid_tool_calls` 的哨兵 `@write_console`**：展开此前只在进程内跑法读 jsonl 之后做
     一次（就地改写用例 dict），子进程吃的是父进程写下的**原始** json ⇒ 哨兵被当字面工具名
     比 ⇒ 36 条用例的"不许写"断言在那一侧**恒不响**；本文件 ⑤ 锁它（锁在判据入口，与跑法
     无关）。

两处的方向都是**放行**（最不该静默的一侧），且都不是"某个跑法写错了"，是"加工判据输入的
那一步只接在了一个跑法上"。共同的治法：**判据自己负责把输入摆对**，不指望调用方记得先加工。

── ①：`rag_search.warm_async()` 挂在 `server.py` 的 **lifespan** 上（只有 HTTP app
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

**锁法**：
- ①–④ 把 `golden_case_runner.main()` 整条驱动起来，`judge_corpus` / `run_case` /
  `check_case` 全换桩（不跑模型、不联网、不落报告），断言：预热发生在**跑用例之前**（顺序，
  不是"文件里有这行"）／判据复用的是**同一份**快照（跑完再取一次 = 允许语料在两次之间变过）／
  预热失败（`None`）**不阻断**评测（用例照跑、判据拿到 `None` ⇒ 判「未评估」，而不是静默通过、
  也不是整轮中止）／进程内跑法也在循环之前预热（源码顺序锁——两个跑法只有一边接上等于没接）。
- ⑤ 直接问判据：**没经过加载期展开**的含哨兵用例喂进 `check_case`，调了写工具它响不响
  （外加反向对照：字面量比对永远不响——那正是静默放行的机制）。

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

print("\n⑤ 哨兵在**判据入口**展开（同一个用例换谁跑都得同一个答案）")
# 病根同上：`forbid_tool_calls: ["@write_console"]` 的展开此前只发生在
# `run_golden.main()` 读完 jsonl 之后（就地改写用例 dict），隔离子进程吃的是父进程写下的
# **原始** json ⇒ 哨兵被当成一个字面工具名去比 ⇒ 36 条用例的"不许写"断言恒不响，方向是
# 放行。这条锁不管跑法，直接问判据：**没有经过加载期展开**的用例喂进来，它响不响。
_wc = sorted(rg.write_console_tools())
check("authz.TOOL_SCOPE 读得到写工具全集（哨兵的单一事实源）", len(_wc) >= 10, f"{len(_wc)} 个")
_probe_case = {"id": "parity_sentinel", "user_input": "删掉那篇文章",
               "gold": {"forbid_tool_calls": ["@write_console"]}}
_probe_res = _stub_result()
_probe_res["tool_calls"] = [_wc[0]]
_pf = rg.check_case(_probe_case, _probe_res)
check("未展开的用例（`@write_console` 还是字面量）调了写工具 ⇒ 判据当场红",
      any("不应调用工具" in f for f in _pf), str(_pf)[:120])
check("  反向对照：字面量比对本身永远不响（「哨兵不展开」就是这么静默放行的）",
      "@write_console" not in [_wc[0]], _wc[0])
_probe_res["tool_calls"] = []
check("  换一条只读调用 ⇒ 同一份用例照样放行（不是把清单当成了「一律有罪」）",
      not rg.check_case(_probe_case, _probe_res), str(rg.check_case(_probe_case, _probe_res))[:120])

print()
if FAILS:
    print(f"失败 {len(FAILS)} 项：" + "；".join(FAILS))
    sys.exit(1)
print("全部通过")
