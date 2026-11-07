# -*- coding: utf-8 -*-
"""Record per-case evaluation traces outside production trace directories."""

import json
import os
import re
import shutil
import time

from config.settings import settings
from utils import trace as trace_mod  # Trace metadata records the tool-result retention limit.

ENV_RUN = "GOLDEN_TRACE_RUN"
ENV_OFF = "GOLDEN_NO_TRACE"

# Only timestamp-shaped run directories qualify for pruning.
_RUN_RE = re.compile(r"^\d{8}_\d{6}$")

# Evaluation trace retention is configurable by the runner.
KEEP_DEFAULT = 30

# Archived evaluation verdicts determine which trace runs may be pruned.
_REPORT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "report", "runs")


def trace_root() -> str:
    """golden trace 根目录 = 生产 trace 目录的**兄弟**（结构上不可能落在它里面）。"""
    return os.path.join(os.path.dirname(os.path.abspath(settings.trace_dir)), "golden_traces")


def enabled() -> bool:
    """Enable evaluation tracing unless GOLDEN_NO_TRACE explicitly disables it."""
    return os.environ.get(ENV_OFF, "").strip().lower() not in ("1", "true", "yes")


# These expectation keys require trace evidence; missing evidence cannot count as a pass.
TRACE_DERIVED_GOLD_KEYS = ("require_ledger_frame", "require_ledger_rows",
                           "require_card_targets_from_ledger")


def trace_derived_keys(case: dict) -> list:
    """Collect expectation keys requiring trace evidence across all case rounds."""
    rounds = case.get("rounds") or [case]
    keys: list = []
    for r in rounds:
        for k in TRACE_DERIVED_GOLD_KEYS:
            if k in (r.get("gold") or {}) and k not in keys:
                keys.append(k)
    return keys


def resolve_run_id(explicit: str | None = None) -> str:
    """Choose an explicit run ID, the environment value or a timestamp, in that order."""
    rid = (explicit or os.environ.get(ENV_RUN) or "").strip()
    return rid or time.strftime("%Y%m%d_%H%M%S")


def case_dir(run_id: str) -> str:
    """Create an evaluation trace directory only after rejecting production trace paths."""
    prod = os.path.abspath(settings.trace_dir)
    d = os.path.abspath(os.path.join(trace_root(), run_id))
    if not d.startswith(os.path.abspath(trace_root()) + os.sep):
        # Reject path traversal and absolute run IDs.
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
    """Count recorded execution calls and distinct execution rounds.
    Requested calls and planner events are not completed tool executions."""
    if not trace_id:
        return [], 0
    calls = [e for e in events_of(trace_id)
             if e.get("node") in ("native", "hybrid") and e.get("event") == "call"
             and e.get("name")]
    return [str(e["name"]) for e in calls], len({e.get("round") for e in calls})


def finish_case(trace_id: str | None, duration_s: float, frames: int = 0) -> str | None:
    """Finalize an evaluation trace and return its path, or None when unavailable.
    Report empty event sets so missing producer-context propagation remains visible."""
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
        print(f"[trace] 保留 {len(kept_fail)} 个有失败的旧 run（失败夜的 trace 是证据，"
              f"不清理）：{kept_fail[0]} … {kept_fail[-1]}")
    return doomed


def _run_verdicts() -> dict[str, bool]:
    """Read known per-run failure verdicts from archived reports.
    Missing or unreadable evidence never marks a run safe for pruning."""
    out: dict[str, bool] = {}
    if not os.path.isdir(_REPORT_DIR):
        return out
    for name in sorted(os.listdir(_REPORT_DIR)):
        if not name.endswith(".json"):
            continue
        try:
            with open(os.path.join(_REPORT_DIR, name), encoding="utf-8") as f:
                doc = json.load(f)
        except Exception:                # noqa: BLE001 -- Unreadable evidence never authorizes pruning.
            continue
        if not isinstance(doc, dict):
            continue
        rid = doc.get("trace_run")
        failed = doc.get("failed")
        if not isinstance(rid, str) or not rid or not isinstance(failed, int):
            continue                     # Missing or malformed verdicts do not prove a clean run.
        reg = doc.get("regression")
        # Keep first-attempt failures even when a rerun passes.
        bad = bool(failed) or (isinstance(reg, dict)
                               and (reg.get("all_passed") is False
                                    or bool(reg.get("flaked_ids"))))
        # 同一个 run_id 可能被多份留档引用：只要有一份说红，就按红算（保守）
        out[rid] = out.get(rid, False) or bad
    return out
