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

# 保留最近多少次 run 的 trace（20260924 从写死的 5 提高；`run_golden --keep-traces`
# 的默认值也取这里——两处各写一个数字就是下一次"改了一处忘了另一处"）
KEEP_DEFAULT = 30

# 留档目录（`eval/report/runs/`）：prune 靠它反查"那一晚是不是红的"。整体 gitignore。
_REPORT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "report", "runs")


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
    （20260922 由 `tests/test_golden_trace.py` 抓出：守卫写错方向，功能整个不可用）。
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
    # tool_result_limit：**这一轮的工具返回在 trace 里被截到多少字符**。落进 trace 是为了
    # 让读的人（尤其 `eval/llm_judge.py` 判"回复有没有编材料"）知道自己手上这份返回文本
    # 是全文还是摘要——看不到这个数就只能靠"长度恰好等于某个整数"猜（20260925：
    # 判官曾拿 200 字符的摘要当完整材料，把文章里真有的「第 3.3 节」判成编造）。
    # 取不到（未设）= 生产默认 200，与 utils/trace.tool_result_text 同源。
    try:
        _lim = int(os.environ.get(trace_mod.TOOL_RESULT_LIMIT_ENV)
                   or trace_mod.TOOL_RESULT_LIMIT_DEFAULT)
    except ValueError:
        _lim = trace_mod.TOOL_RESULT_LIMIT_DEFAULT
    start_trace(tid, 0, "golden_thread",
                {"golden": True, "run": run_id, "case": case_id, "role": role,
                 "message": (message or "")[:200], "tool_result_limit": _lim},
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


def prune(keep: int = KEEP_DEFAULT) -> list[str]:
    """只保留最近 `keep` 个 run 目录（**外加所有有失败的旧 run**），返回被删的名单。

    目录名是时间戳 ⇒ 字典序即时间序。**只删形如 `<8位日期>_<6位时刻>` 的目录**：
    根目录下别的东西（手放的样本、临时的 .tmp）一概不碰。`keep<=0` 表示不清理。

    20260924 两条改动（动机：判红的用例只能靠"复采样几次看是不是方差"来裁决，而它
    的 trace 恰恰是被清掉的那一批）：

      · 默认 `keep` 从 5 提到 `KEEP_DEFAULT`——一次全量约 50KB，30 次也就一两兆，
        真正稀缺的不是盘而是"红那次到底 planner 怎么决策的"；
      · **有失败的 run 永久保留**：判据不靠目录名撞报告名，而是**反查留档**——
        `eval/report/runs/*.json` 里的 `trace_run` 就是这份报告的 trace 目录，那份
        报告自己写着 `failed` / `regression.all_passed`（`flaked_ids` 同样算"不干净"：
        首跑红复跑绿的那一夜，首跑 trace 是唯一能回答"为什么红"的东西）。
        **只有能证明那晚是干净的才删**：
        留档说失败 → 留；留档缺失、字段不认识（旧格式）、JSON 读不出来 → 同样留
        （证据不足就不删，缺的正是排障时要看的那份）。故意的：宁可多留几个目录。
    """
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
        # 调用方（run_golden / golden_full_run）只打印"删了几个"，留下的这批得自己吭声：
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
        # 首跑红复跑绿（`flaked_ids`，20260924）同样算"这一晚不干净" ⇒ 留着：门禁虽然
        # 按方差放行了，但那一份首跑 trace 是**唯一**能回答"首跑为什么红"的东西。
        bad = bool(failed) or (isinstance(reg, dict)
                               and (reg.get("all_passed") is False
                                    or bool(reg.get("flaked_ids"))))
        # 同一个 run_id 可能被多份留档引用：只要有一份说红，就按红算（保守）
        out[rid] = out.get(rid, False) or bad
    return out
