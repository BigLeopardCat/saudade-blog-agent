# -*- coding: utf-8 -*-
"""golden set 的 trace 落盘（20260922）：**每条用例一份，落独立目录**。

为什么要有：golden 是**进程内**直调链路（`run_one` → `_run_agent_stream_to_queue`），
而 `start_trace` 只在 `server.py` 的 `chat_stream` 里调 ⇒ golden 跑出来**一条 trace 都没有**。
后果是判红的用例只能靠"复采样几次看是不是方差"来裁决（20260922 实测：
`admin_write_intent_tag_remove_popup` 5 跑 3 绿，最后还得线下复现才敢下结论），而 trace 里
本来就有 planner 原始决策、被剔的清单项、gate 打回原因、四段耗时——一次读记录就够。
效率四指标（工具调用/规划轮/多绕圈/重复检索）也本来就只从 trace 算，golden 因此一个都不产。

三条纪律（写在代码里，别只写在文档里）：

1. **绝不落生产 trace 目录**（`settings.trace_dir`）：`trace_alert.py`/`trace_metrics.py`/
   效率基线扫的就是那里，评测流量混进去等于污染自己的判据；那个目录还归 logrotate 管
   （`traces/*.json` 是独立块 rename+compress），一次上百份 × 多跑会改掉保留语义。
   ⇒ 落它的**兄弟目录** `golden_traces/<run_id>/`，`case_dir()` 里带拒绝守卫。
2. **trace 里的 user_id 一律写 0**：那几条 `needs_admin_uid` 用例带真管理员 uid，而身份
   是测试夹具、不是真人——真 uid 既不进字段也不进文件名。
3. **一个 run 一个目录**：清理按目录整体删（保留最近 N 次），文件名 = `<case_id>.json`，
   报告里带路径 ⇒ 红条直接指着那份 trace 读。

用法（`run_golden.py` 与 `eval/golden_case_runner.py` 共用）：`start_case(...)` → 跑一轮 →
`finish_case(...)`。进程隔离跑法（`golden_full_run.py`）靠环境变量 `GOLDEN_TRACE_RUN`
把同一个 run_id 传给每个子进程；`GOLDEN_NO_TRACE=1` 整体关掉。

**start 必须在这一轮被提交给线程池之前调**：recorder 挂在 contextvar 上，靠
`_submit_with_context` 的 copy_context 传进 producer 线程；晚一步调就是一份空壳 trace。
"""

import os
import re
import shutil
import time

from config.settings import settings

ENV_RUN = "GOLDEN_TRACE_RUN"
ENV_OFF = "GOLDEN_NO_TRACE"

# run 目录名 = 时间戳（`%Y%m%d_%H%M%S`）；清理只认这个形状
_RUN_RE = re.compile(r"^\d{8}_\d{6}$")


def trace_root() -> str:
    """golden trace 根目录 = 生产 trace 目录的**兄弟**（结构上不可能落在它里面）。"""
    return os.path.join(os.path.dirname(os.path.abspath(settings.trace_dir)), "golden_traces")


def enabled() -> bool:
    """默认开（不给 trace 的评测等于放弃排障）；`GOLDEN_NO_TRACE=1` 关。"""
    return os.environ.get(ENV_OFF, "").strip().lower() not in ("1", "true", "yes")


def resolve_run_id(explicit: str | None = None) -> str:
    """run_id：显式参数 > 环境变量（进程隔离跑法由父进程给）> 当前时刻。"""
    rid = (explicit or os.environ.get(ENV_RUN) or "").strip()
    return rid or time.strftime("%Y%m%d_%H%M%S")


def case_dir(run_id: str) -> str:
    """本次 run 的目录（按需创建）。**在创建之前**核对它不在生产 trace 目录里。

    注意比的是**生产 trace 目录本身**（`settings.trace_dir`），不是它的父目录——
    golden 根正是那个父目录下的兄弟目录，拿父目录比会把每一次 golden 落盘都拒掉
    （20260922 由 `test_golden_trace.py` 抓出：守卫写错方向，功能整个不可用）。
    """
    prod = os.path.abspath(settings.trace_dir)
    d = os.path.abspath(os.path.join(trace_root(), run_id))
    if not d.startswith(os.path.abspath(trace_root()) + os.sep):
        # run_id 里带 `..`/绝对路径 ⇒ 拒绝（路径是拼出来的，别让参数把它带出去）
        raise RuntimeError(f"golden trace 目录越界：{d!r}（run_id={run_id!r}）")
    if d == prod or d.startswith(prod + os.sep):
        raise RuntimeError(f"golden trace 不能落生产目录：{d!r}")
    os.makedirs(d, exist_ok=True)
    return d


def start_case(run_id: str, case_id: str, message: str = "", role: str | None = None) -> str | None:
    """建 recorder 并返回 trace_id（关掉时返回 None）。

    调用点必须在 `ex.submit` 之前（见模块头注：晚一步就是空壳 trace）。
    """
    if not enabled():
        return None
    from utils.trace import start_trace

    tid = f"{run_id}__{case_id}"
    start_trace(tid, 0, "golden_thread",
                {"golden": True, "run": run_id, "case": case_id, "role": role,
                 "message": (message or "")[:200]},
                dir=case_dir(run_id), name=case_id)
    return tid


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


def prune(keep: int = 5) -> list[str]:
    """只保留最近 `keep` 个 run 目录，返回被删的名单。

    目录名是时间戳 ⇒ 字典序即时间序。**只删形如 `<8位日期>_<6位时刻>` 的目录**：
    根目录下别的东西（手放的样本、临时的 .tmp）一概不碰。`keep<=0` 表示不清理。
    """
    if keep <= 0:
        return []
    root = trace_root()
    if not os.path.isdir(root):
        return []
    runs = sorted(n for n in os.listdir(root)
                  if _RUN_RE.match(n) and os.path.isdir(os.path.join(root, n)))
    doomed = runs[:-keep] if len(runs) > keep else []
    for n in doomed:
        shutil.rmtree(os.path.join(root, n), ignore_errors=True)
    return doomed
