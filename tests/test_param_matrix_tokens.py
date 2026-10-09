# -*- coding: utf-8 -*-
"""`param_matrix` 的**用量列**（输入tok / 命中率）离线锁（20261010）。

**被锁的缺陷**：`eval/report/param_matrix.jsonl` 的行此前只有红数/下界/延迟/工具调用数
——**换模型那一格最该看的两件东西都缺**（`docs/param-tuning-20261006.md` §八.4）。而这一层
的失效方式与 `tests/test_golden_arm.py` 同族：**全都不报错**。

 ① **缺席被写成 0**：trace 目录不在 / 这遍根本没落 trace 时，把 `input_tok` 写成 `0`
    就是在说"这一遍没花 token"——本仓在缓存字段上踩过同一个坑（「量不到」与「命中 0 次」
    是两件事，见 `agent/llm_usage.py`）。判据因此**认 `None`**。
 ② **命中率被就地重算**：率只有一个实现（`token_cost_report.totals`，与它 `main()` 打的
    合计行同式）。在 `param_matrix` 里自己再除一遍 ⇒ 两处的口径会各漂各的
    （`cache_seen` 是闸，分母是输入 tok 总数——差一个字段就是一次静默的读数漂移）。
 ③ **接线没测**：`_row()` 把用量并进行里、`report()` 把两列摆上表——能力有测试 ≠ 接线有
    测试（本仓反复吃过这个）。

秒级、零网络、零 LLM、零生产写入：全部走 `tempfile` 造的假 trace 目录 + 猴补
`param_matrix.TRACE_ROOT` / `MATRIX`，**不读**生产 trace、**不动** `param_matrix.jsonl`。
"""
import io
import json
import shutil
import sys
import tempfile
from contextlib import redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent   # 仓根（tests/ 的上一层）
EVAL = ROOT / "eval"
sys.path.insert(0, str(EVAL))

import param_matrix as pm  # noqa: E402

FAILS: list[str] = []


def check(desc: str, cond: bool, detail: str = "") -> None:
    print(("  ✅ " if cond else "  ❌ ") + desc + (f"  [{detail}]" if detail else ""))
    if not cond:
        FAILS.append(desc)


def _src(name: str) -> str:
    return (EVAL / name).read_text(encoding="utf-8")


def _code(name: str) -> str:
    """源码**剥掉注释**再判「没有」（同 `test_golden_arm.py`：判的是"代码里不许再这么
    写"，不是"文档里不许提它"——注释里往往正是旧写法的样子）。"""
    out = []
    for ln in _src(name).splitlines():
        s = ln.lstrip()
        if s.startswith("#"):
            continue
        i = ln.find(" #")
        out.append(ln[:i] if i >= 0 else ln)
    return "\n".join(out)


def _ev(node="planner", inp=1000, out=10, cache=None):
    e = {"node": node, "engine": "q", "input": inp, "output": out}
    if cache is not None:
        e["cache_read"] = cache
    return e


def _mkrun(root: Path, name: str, traces: dict) -> Path:
    """造一个 run 目录 + 若干份 trace（值 = events 列表，字符串则原样落盘当坏文件）。"""
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    for fn, ev in traces.items():
        p = d / fn
        if isinstance(ev, str):
            p.write_text(ev, encoding="utf-8")
        else:
            p.write_text(json.dumps({"events": ev}), encoding="utf-8")
    return d


def _fake_report(trace_run):
    """一份**够 `_row()` 读**的最小报告（字段名照 `eval/run_golden.py` 的产出）。"""
    return {
        "ts": "2026-10-10 12:00:00", "trace_run": trace_run,
        "total": 188, "passed": 180, "failed": 8,
        "landing": {"sampled": {"total": 161, "passed": 153, "lower": 0.9050,
                                "point": 0.9503, "target": 0.95,
                                "state": "ok", "failed_ids": ["x"]},
                    "hard": {"ok": True}},
        "regression": {"failed_ids": []},
        "efficiency": {"resets_total": 0}, "plan_efficiency": {"tool_calls_total": 200},
        "latency_s": {"p50": 5.6, "p95": 14.6}, "engine": "native",
    }


def _write(path: Path, trace_run: str) -> Path:
    """落一份假报告，返回它的路径（`_row()` 吃路径）。"""
    path.write_text(json.dumps(_fake_report(trace_run)), encoding="utf-8")
    return path


_ROOT = Path(tempfile.mkdtemp(prefix="pmtok_"))
_TMPROOT = _ROOT / "traces"
_TMPROOT.mkdir()
_pm_trace_root = pm.TRACE_ROOT
_pm_matrix = pm.MATRIX
pm.TRACE_ROOT = _TMPROOT                       # 猴补：本套件绝不读生产 trace

try:
    print("① 缺席 ⇒ 全部 `None`（**不是 0**）：没量到 ≠ 用量 0")
    check("`trace_run` 为空串",
          all(pm._token_usage("", root=_TMPROOT)[k] is None for k in pm._TOKEN_KEYS))
    check("`trace_run` 为 None（跑挂了那行就是这么来的）",
          all(pm._token_usage(None, root=_TMPROOT)[k] is None for k in pm._TOKEN_KEYS))
    check("目录不存在（trace 已被 30 天保留期清掉）",
          all(pm._token_usage("20200101_000000", root=_TMPROOT)[k] is None
              for k in pm._TOKEN_KEYS))
    _mkrun(_TMPROOT, "20200102_000000", {})    # 目录在、一份 trace 都没有
    check("目录在但一份 trace 都没有（仍是「没量到」）",
          all(pm._token_usage("20200102_000000", root=_TMPROOT)[k] is None
              for k in pm._TOKEN_KEYS))

    print("\n② 正常一遍：数得出来（率的**唯一**实现是 `token_cost_report.totals`）")
    _mkrun(_TMPROOT, "20261010_120000", {
        "20261010T120000_1_aaaaaaaa.json": [
            _ev(inp=1000, out=10, cache=800),      # planner，报了缓存字段
            _ev(node="model", inp=100, out=3),     # narrator，没报
        ],
        "20261010T120001_2_bbbbbbbb.json": [
            _ev(inp=500, out=5, cache=400),
        ],
    })
    u = pm._token_usage("20261010_120000", root=_TMPROOT)
    check("LLM 调用数 = 3", u["llm_calls"] == 3, str(u["llm_calls"]))
    check("输入 tok = 1600 / 输出 tok = 18",
          u["input_tok"] == 1600 and u["output_tok"] == 18,
          f"{u['input_tok']}/{u['output_tok']}")
    check("命中 tok = 1200 / 报了缓存字段的调用 = 2",
          u["cache_hit_tok"] == 1200 and u["cache_seen"] == 2,
          f"{u['cache_hit_tok']}/{u['cache_seen']}")
    check("命中率 = 命中 tok / 输入 tok = 0.75（分母**不是**调用数）",
          u["cache_hit_rate"] == 0.75, str(u["cache_hit_rate"]))
    check("率与 `token_cost_report.totals` 同式（那边换了口径这里会红）",
          u["cache_hit_rate"] == round(1200 / 1600, 4))

    print("\n③ 一次都没报缓存字段：命中率是 `None`，但用量照报")
    _mkrun(_TMPROOT, "20261010_130000", {
        "20261010T130000_1_cccccccc.json": [_ev(inp=1000, out=10), _ev(inp=600, out=6)],
    })
    u2 = pm._token_usage("20261010_130000", root=_TMPROOT)
    check("调用数与输入 tok **照报**（这遍确实跑了）",
          u2["llm_calls"] == 2 and u2["input_tok"] == 1600,
          f"{u2['llm_calls']}/{u2['input_tok']}")
    check("`cache_seen` = 0（真观测：0 次调用报了该字段）", u2["cache_seen"] == 0)
    check("`cache_hit_rate` 是 `None` 而不是 `0.0`",
          u2["cache_hit_rate"] is None, repr(u2["cache_hit_rate"]))
    check("`cache_hit_tok` 也是 `None`（写 0 就是在说「一次都没命中」）",
          u2["cache_hit_tok"] is None, repr(u2["cache_hit_tok"]))

    print("\n④ 坏文件不炸、也不改口径（`trace_io.load_trace` 的 None 语义）")
    _mkrun(_TMPROOT, "20261010_140000", {
        "20261010T140000_1_dddddddd.json": [_ev(inp=100, out=1, cache=50)],
        "20261010T140001_2_eeeeeeee.json": "{ 这不是 json",
    })
    u3 = pm._token_usage("20261010_140000", root=_TMPROOT)
    check("坏文件被跳过、好文件照算（1 次调用 / 100 输入）",
          u3["llm_calls"] == 1 and u3["input_tok"] == 100,
          f"{u3['llm_calls']}/{u3['input_tok']}")

    print("\n⑤ `_row()` 集成：用量并进**行**里（这是接线，不是能力）")
    rp_with = _ROOT / "fake_with_tokens.json"
    rp_with.write_text(json.dumps(_fake_report("20261010_120000")), encoding="utf-8")
    row = pm._row("live", 1, 0, rp_with)
    check("行里有全部用量键（缺键会让报表按 `—` 排，静默降级）",
          all(k in row for k in pm._TOKEN_KEYS),
          str([k for k in pm._TOKEN_KEYS if k not in row]))
    check("行的用量值取自报告里的 `trace_run`",
          row.get("llm_calls") == 3 and row.get("cache_hit_rate") == 0.75,
          f"{row.get('llm_calls')}/{row.get('cache_hit_rate')}")

    rp_none = _ROOT / "fake_no_cache.json"
    rp_none.write_text(json.dumps(_fake_report("20261010_130000")), encoding="utf-8")
    row2 = pm._row("live", 2, 0, rp_none)
    check("没报缓存字段时行里是 `None`（不是 0）",
          row2.get("cache_hit_rate") is None and row2.get("cache_hit_tok") is None
          and row2.get("llm_calls") == 2)

    rp_gone = _ROOT / "fake_gone.json"
    rp_gone.write_text(json.dumps(_fake_report("20200101_000000")), encoding="utf-8")
    row3 = pm._row("live", 3, 0, rp_gone)
    check("trace 目录已不在时**键齐、值全 None**（老行在读取端才补得出来）",
          all(k in row3 and row3[k] is None for k in pm._TOKEN_KEYS))
    err_row = pm._row("live", 4, 1, None)
    check("**没产出报告**那行也带齐用量键（否则报表的分列会错位）",
          all(k in err_row and err_row[k] is None for k in pm._TOKEN_KEYS)
          and err_row.get("error"))

    print("\n⑥ `report()`：两列真上表；老行（无用量键）在**读取端**按 `trace_run` 补")
    # 量级照真读数取（一遍 ~9–10M 输入）：太小的话 `:.2f}M` 会一律打成 `0.00M`，
    # 那些断言就退化成"看谁都是零"了。
    _mkrun(_TMPROOT, "20261010_150000", {
        "20261010T150000_1_ffffffff.json": [
            _ev(inp=9_710_000, out=56_000, cache=7_900_000),
            _ev(node="model", inp=100_000, out=500),           # 没报缓存字段
        ],
    })
    _mkrun(_TMPROOT, "20261010_160000", {
        "20261010T160000_1_11111111.json": [
            _ev(inp=8_880_000, out=48_000, cache=7_200_000),
        ],
    })
    row_new = pm._row("live", 1, 0, _write(_ROOT / "r_a.json", "20261010_150000"))
    # 老行 = **本列上线前**写的那种行：形状一模一样，就是没有那 7 个用量键
    # （直接剥，别手搓——手搓出来的行会缺 `sampled_total`，被报表当半截轮排除，
    # 那样测的是"排除了"，不是"读取端补出来了"）。
    row_old = {k: v for k, v in
               pm._row("live", 2, 0, _write(_ROOT / "r_b.json", "20261010_160000")).items()
               if k not in pm._TOKEN_KEYS}
    row_gone = {k: v for k, v in
                pm._row("live", 3, 0, _write(_ROOT / "r_c.json", "20200101_000000")).items()
                if k not in pm._TOKEN_KEYS}
    _mat = _ROOT / "matrix.jsonl"
    _mat.write_text("\n".join(json.dumps(r, ensure_ascii=False)
                              for r in (row_new, row_old, row_gone)) + "\n",
                    encoding="utf-8")
    pm.MATRIX = _mat
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = pm.report(only=["live"])
    pm.MATRIX = _pm_matrix
    out = buf.getvalue()
    check("report 正常返回", rc == 0, str(rc))
    check("表头有两列新指标", "输入tok" in out and "命中率" in out)
    check("新行的用量按原值打印（9.81M / 80.5%）", "9.81M" in out and "80.5%" in out)
    check("**老行**在读取端按 `trace_run` 补出用量（8.88M / 81.1%）",
          "8.88M" in out and "81.1%" in out)
    check("trace 已不在的第三遍是 `—`（**不是** `0.00M` / `0.0%`）",
          "| — |" in out and "0.00M" not in out)
    check("有调用没报缓存字段时**点名**（否则那个率读的人不知道它偏低）",
          "没报缓存字段" in out)

    print("\n⑦ 接线锁：率**只有一个实现**，`param_matrix` 只调用、不重算")
    check("`param_matrix` 引的是 `token_cost_report.totals`（唯一的聚合实现）",
          "token_cost_report.totals(" in _code("param_matrix.py"))
    check("枚举走 `trace_files.iter_trace_files`（唯一的枚举入口），不自己 glob trace",
          "trace_files.iter_trace_files(" in _code("param_matrix.py"))
    check("`param_matrix` **不自己数**缓存字段（代码里不该出现那个事件字段名）",
          "cache_read" not in _code("param_matrix.py"))
    check("`totals` 仍住在 `token_cost_report`（搬走 = 两处口径各漂各的）",
          "def totals(" in _src("token_cost_report.py"))
    check("率的式子在 `totals` 里只出现一次（`param_matrix` 不许再除一遍）",
          _src("token_cost_report.py").count('tot["cache"] / tot["in"]') == 1)
finally:
    pm.TRACE_ROOT = _pm_trace_root
    shutil.rmtree(_ROOT, ignore_errors=True)

print("\n" + ("全部通过" if not FAILS else f"失败 {len(FAILS)} 项：" + "; ".join(FAILS)))
raise SystemExit(1 if FAILS else 0)
