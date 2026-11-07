# -*- coding: utf-8 -*-
"""golden set 的 trace 落盘：**每条用例一份，落独立目录**。
"""

import json
import os
import re
import shutil
import time

from config.settings import settings
from utils import trace as trace_mod  # 工具返回在 trace 里留多长（写进 input 供读的人判断）

ENV_RUN = "GOLDEN_TRACE_RUN"
ENV_OFF = "GOLDEN_NO_TRACE"

# run 目录名 = 时间戳（`%Y%m%d_%H%M%S`）；清理只认这个形状
_RUN_RE = re.compile(r"^\d{8}_\d{6}$")

# Evaluation trace retention is configurable by the runner.
KEEP_DEFAULT = 30

# 留档目录（`eval/report/runs/`）：prune 靠它反查"那一晚是不是红的"。整体 gitignore。
_REPORT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "report", "runs")


def trace_root() -> str:
    """golden trace 根目录 = 生产 trace 目录的**兄弟**（结构上不可能落在它里面）。"""
    return os.path.join(os.path.dirname(os.path.abspath(settings.trace_dir)), "golden_traces")


def enabled() -> bool:
    """默认开（不给 trace 的评测等于放弃排障）；`GOLDEN_NO_TRACE=1` 关。"""
    return os.environ.get(ENV_OFF, "").strip().lower() not in ("1", "true", "yes")


# These expectation keys require trace evidence; missing evidence cannot count as a pass.
TRACE_DERIVED_GOLD_KEYS = ("require_ledger_frame", "require_ledger_rows",
                           "require_card_targets_from_ledger")


def trace_derived_keys(case: dict) -> list:
    """这条用例（**多轮也算**）里靠 trace 事件才判得动的 gold 键。

    轮次取法与 `golden_evaluation.iter_rounds` 同源（单轮用例字段在顶层，多轮写在 `rounds` 里）
    ——这里只读 `gold` 一格，故不复用那个归一化器（它会连带构造 context，代价不对等）。
    """
    rounds = case.get("rounds") or [case]
    keys: list = []
    for r in rounds:
        for k in TRACE_DERIVED_GOLD_KEYS:
            if k in (r.get("gold") or {}) and k not in keys:
                keys.append(k)
    return keys


def resolve_run_id(explicit: str | None = None) -> str:
    """run_id：显式参数 > 环境变量（进程隔离跑法由父进程给）> 当前时刻。"""
    rid = (explicit or os.environ.get(ENV_RUN) or "").strip()
    return rid or time.strftime("%Y%m%d_%H%M%S")


def case_dir(run_id: str) -> str:
    """Create an evaluation trace directory only after rejecting production trace paths."""
    prod = os.path.abspath(settings.trace_dir)
    d = os.path.abspath(os.path.join(trace_root(), run_id))
    if not d.startswith(os.path.abspath(trace_root()) + os.sep):
        # run_id 里带 `..`/绝对路径 ⇒ 拒绝（路径是拼出来的，别让参数把它带出去）
        raise RuntimeError(f"golden trace 目录越界：{d!r}（run_id={run_id!r}）")
    if d == prod or d.startswith(prod + os.sep):
        raise RuntimeError(f"golden trace 不能落生产目录：{d!r}")
    os.makedirs(d, exist_ok=True)
    return d


def start_case(run_id: str, case_id: str, message: str = "", role: str | None = None,
               thread: str = "golden_thread", runtime: dict | None = None) -> str | None:
    """Start an isolated case recorder, returning None when evaluation tracing is disabled."""
    if not enabled():
        return None
    from utils.trace import start_trace

    tid = f"{run_id}__{case_id}"
    # A global evaluation result limit overrides per-tool production defaults.
    try:
        _lim = int(os.environ.get(trace_mod.TOOL_RESULT_LIMIT_ENV)
                   or trace_mod.TOOL_RESULT_LIMIT_DEFAULT)
    except ValueError:
        _lim = trace_mod.TOOL_RESULT_LIMIT_DEFAULT
    start_trace(tid, 0, thread,
                {"source": "evaluation", "runtime": runtime or {},
                 "golden": True, "run": run_id, "case": case_id, "role": role,
                 "message": (message or "")[:200], "tool_result_limit": _lim},
                dir=case_dir(run_id), name=case_id,
                # Evaluation traces use their run directory instead of production day partitions.
                by_day=False)
    return tid


def events_of(trace_id: str) -> list:
    """Read a snapshot of the active evaluation recorder before it is finalized."""
    from utils.trace import _ACTIVE

    recorder = _ACTIVE.get(trace_id)
    return list(recorder.events) if recorder is not None else []


def tool_call_stats(trace_id: str | None) -> tuple[list[str], int]:
    """Read actual native calls before finish_case removes the recorder.

    Raw tool messages stay inside the model loop. Historical hybrid records
    remain readable, but planner events and requested calls are not executions.
    """
    if not trace_id:
        return [], 0
    calls = [e for e in events_of(trace_id)
             if e.get("node") in ("native", "hybrid") and e.get("event") == "call"
             and e.get("name")]
    return [str(e["name"]) for e in calls], len({e.get("round") for e in calls})


def finish_case(trace_id: str | None, duration_s: float, frames: int = 0) -> str | None:
    """收尾落盘，返回路径（没开/写失败 → None）。

    **事件为空要吭声**：contextvar 没传进 producer 线程时，落下来的是一份只有元数据的
    空壳，静默当成功就是又一次"看起来在记录"（与探针"查错层把 PASS 报成 FAIL"同源）。
    """
    if not trace_id:
        return None
    from utils.trace import _ACTIVE, finish_trace

    rec = _ACTIVE.get(trace_id)
    n_events = len(rec.events) if rec is not None else 0
    path = finish_trace(trace_id, "golden_done", duration_s, frames)
    if path and n_events == 0:
        print(f"[trace] ⚠ {os.path.basename(path)} 事件为空"
              f"（start 晚于提交、或 contextvar 未传到 producer 线程）")
    return path


def prune(keep: int = KEEP_DEFAULT) -> list[str]:
    """Prune recognized timestamp directories beyond the retention count.

    Preserve failed, flaky and unverified runs. Missing or unreadable reports
    do not prove success and therefore do not authorize trace deletion.
    Nonpositive keep values disable pruning; unrelated directories are untouched."""
    if keep <= 0:
        return []
    root = trace_root()
    if not os.path.isdir(root):
        return []
    runs = sorted(n for n in os.listdir(root)
                  if _RUN_RE.match(n) and os.path.isdir(os.path.join(root, n)))
    doomed = runs[:-keep] if len(runs) > keep else []
    verdict = _run_verdicts()
    kept_fail = [n for n in doomed if verdict.get(n) is not False]
    doomed = [n for n in doomed if verdict.get(n) is False]
    for n in doomed:
        shutil.rmtree(os.path.join(root, n), ignore_errors=True)
    if kept_fail:
        # Retained evidence must be visible in the cleanup report.
        # 悄悄留下 = 下次没人知道为什么这个目录还在
        print(f"[trace] 保留 {len(kept_fail)} 个有失败的旧 run（失败夜的 trace 是证据，"
              f"不清理）：{kept_fail[0]} … {kept_fail[-1]}")
    return doomed


def _run_verdicts() -> dict[str, bool]:
    """留档反查：`{run_id: 那晚有没有失败}`（只在能确定时进字典）。

    读者是纯函数式的——目录路径走模块常量 `_REPORT_DIR`（测试 monkeypatch 到 tmpdir，
    照 `test_golden_trace` 既有约定：离线测试必须能把目录指走、生产目录零写入）。
    """
    out: dict[str, bool] = {}
    if not os.path.isdir(_REPORT_DIR):
        return out
    for name in sorted(os.listdir(_REPORT_DIR)):
        if not name.endswith(".json"):
            continue
        try:
            with open(os.path.join(_REPORT_DIR, name), encoding="utf-8") as f:
                doc = json.load(f)
        except Exception:                # noqa: BLE001 —— 读不出=证据不足，跳过（对应"留"）
            continue
        if not isinstance(doc, dict):
            continue
        rid = doc.get("trace_run")
        failed = doc.get("failed")
        if not isinstance(rid, str) or not rid or not isinstance(failed, int):
            continue                     # 旧留档没有 trace_run / 形态不认识 ⇒ 不当成"干净"
        reg = doc.get("regression")
        # Keep first-attempt failures even when a rerun passes.
        bad = bool(failed) or (isinstance(reg, dict)
                               and (reg.get("all_passed") is False
                                    or bool(reg.get("flaked_ids"))))
        # 同一个 run_id 可能被多份留档引用：只要有一份说红，就按红算（保守）
        out[rid] = out.get(rid, False) or bad
    return out
