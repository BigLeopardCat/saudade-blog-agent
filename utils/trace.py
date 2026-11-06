"""Per-request structured events, finalized after the actual worker jobs exit.

The recorder follows request context into workers. RequestJobs retains the trace
until every job drains, so late cancellation and usage events are included before
atomic file publication. Model/tool results use bounded, visibly marked excerpts.
"""

import contextvars
import json
import logging
import os
import threading
import time
from datetime import datetime

from config.settings import settings

logger = logging.getLogger(__name__)

# 当前请求的 recorder（无值时 None；record 静默跳过——非流式请求不建 trace）
_recorder: contextvars.ContextVar = contextvars.ContextVar("trace_recorder", default=None)

# trace_id → recorder：event_stream（asyncio）与 producer 线程（threading）跨
# 执行模型中转，进程内单例；CPython GIL 下 get/pop 原子，无需额外同步。
_ACTIVE: dict = {}
_LOCK = threading.Lock()

# Runtime configuration is the single source for production trace storage.
TRACE_DIR = settings.trace_dir

# A per-run override applies to every tool; production uses bounded excerpts.
TOOL_RESULT_LIMIT_ENV = "TRACE_TOOL_RESULT_LIMIT"
TOOL_RESULT_LIMIT_DEFAULT = 4000
# Evaluation material can retain longer evidence without changing model context.
GOLDEN_MATERIAL_LIMIT = 40000

# Retain maximum-size article pages with source metadata; operator overrides still apply.
TOOL_RESULT_LIMITS: dict[str, int] = {"get_article_detail": 32000}
NO_LIMIT_TOOLS = frozenset({"rag_search"})
TRUNCATION_MARK_PREFIX = "…[trace 截断：原文共 "

# Optional fields/events are additive. Increment for changed field semantics.
# Missing schema_version denotes older retained traces.
SCHEMA_VERSION = 1

# These stage markers survive a process exit even if final trace publication cannot run.
_LOGGED_STAGES = frozenset({"caller_context", "llm_start", "llm_done", "llm_error",
                            "model_first_token", "tool_requested", "checkpoint_write",
                            "consumer_closed", "jobs_drained"})


def truncation_mark(original_len: int) -> str:
    return f"{TRUNCATION_MARK_PREFIX}{original_len} 字符]"


def is_truncated(text: str) -> bool:
    """这段落进 trace 的返回文本**被截断过**吗（标记说了算，不靠长度猜）。"""
    return TRUNCATION_MARK_PREFIX in str(text)


def tool_result_text(result: str, tool: str = "", *, default_limit=None) -> str:
    """工具返回 → 写进 trace 的文本。分档见 TOOL_RESULT_LIMITS / NO_LIMIT_TOOLS。

    限值语义（三层，从高到低）：
      ① 环境变量 `TRACE_TOOL_RESULT_LIMIT` 设了 ⇒ **全局**按它（正数=留这么多；≤0=不截断；
         值写坏了 ⇒ 告警并**退回分档**，不是悄悄放开也不是悄悄砍到 200）；
      ② 否则看单工具覆盖 `TOOL_RESULT_LIMITS`（≤0 = 不截断）；
      ③ 否则 `TOOL_RESULT_LIMIT_DEFAULT`。
    `NO_LIMIT_TOOLS` 里的工具在任何情况下都不截断。截断时**带标记**（`is_truncated` 可判）。
    """
    text = str(result)
    if tool in NO_LIMIT_TOOLS:
        return text
    raw = os.environ.get(TOOL_RESULT_LIMIT_ENV)
    limit: int | None = None
    if raw:
        try:
            limit = int(raw)
        except ValueError:                   # 值写坏了 ⇒ 退回分档（告警，不静默）
            logger.warning("[trace] %s=%r 不是整数，工具返回按分档上限落盘",
                           TOOL_RESULT_LIMIT_ENV, raw)
    if limit is None:
        limit = TOOL_RESULT_LIMITS.get(tool, TOOL_RESULT_LIMIT_DEFAULT
                                      if default_limit is None else default_limit)
    if limit <= 0 or len(text) <= limit:
        return text
    return text[:limit] + truncation_mark(len(text))


class _TraceRecorder:
    def __init__(self, trace_id: str, user_id: int, thread_id: str, input_meta: dict,
                 trace_dir: str | None = None, name: str | None = None,
                 by_day: bool = True):
        self.trace_id = trace_id
        self.process_id = os.getpid()
        self.user_id = user_id
        self.thread_id = thread_id
        # Explicit evaluation paths stay separate from production traces.
        self.trace_dir = trace_dir or TRACE_DIR
        self.name = name or ""
        self.by_day = by_day
        self.started_at = datetime.now().astimezone().isoformat(timespec="seconds")
        self._t0 = time.monotonic()
        self.input_meta = input_meta
        self.events: list = []
        self.reply = ""
        # Filled by finalize(); None keeps "not finished yet" distinct from an empty value.
        self.end_reason: str | None = None
        self.duration_s: float | None = None
        self.frames = 0
        self.dumped = False
        logger.info("trace_id=%s stage=request_started", self.trace_id)

    def record(self, node: str, event: str, **data) -> None:
        if self.dumped:
            return
        # 相对请求开始的单调偏移秒——跨事件排序/分段耗时直接读差值
        if event in _LOGGED_STAGES:
            logger.info("trace_id=%s stage=%s.%s", self.trace_id, node, event)
        self.events.append({"t": round(time.monotonic() - self._t0, 3),
                            "node": node, "event": event, **data})

    def set_reply(self, reply: str) -> None:
        self.reply = (reply or "")[:2000]

    def finalize(self, end_reason: str, duration_s: float, frames: int) -> None:
        self.end_reason = end_reason
        self.duration_s = round(duration_s, 1)
        self.frames = frames
        # reply 顶层字段：producer 收尾的 stream_end 事件带最终回复正文
        # （事件与顶层字段双写，阅读者两个位置都能取到）
        for ev in self.events:
            if ev.get("event") == "stream_end" and ev.get("reply"):
                self.reply = ev["reply"][:2000]
                break

    def dump(self) -> str | None:
        """落盘并返回文件路径（写失败返回 None——调用方不能假定一定写成功）。"""
        if self.dumped:
            return None
        self.dumped = True
        try:
            doc = {
                "schema_version": SCHEMA_VERSION,
                "trace_id": self.trace_id,
                "process_id": self.process_id,
                "user_id": self.user_id,
                "thread_id": self.thread_id,
                "started_at": self.started_at,
                "duration_s": self.duration_s,
                "end_reason": self.end_reason,
                "frames": self.frames,
                "input": self.input_meta,
                "reply": self.reply,
                "events": self.events,
            }
            stamp = datetime.fromisoformat(self.started_at).strftime("%Y%m%dT%H%M%S")
            if self.name:
                fname = f"{self.name}.json"
            else:
                fname = f"{stamp}_{self.user_id}_{self.trace_id[:8]}.json"
            out_dir = os.path.join(self.trace_dir, stamp[:8]) if self.by_day else self.trace_dir
            # Trace directories and files are private; readers only see atomic publication.
            os.makedirs(out_dir, mode=0o700, exist_ok=True)
            path = os.path.join(out_dir, fname)
            tmp = path + ".tmp"  # 原子替换：reader 不会读到半截文件
            with open(tmp, "w", encoding="utf-8",
                      opener=lambda target, flags: os.open(target, flags, 0o600)) as f:
                json.dump(doc, f, ensure_ascii=False, indent=1)
            os.replace(tmp, path)
            return path
        except Exception:
            logger.exception("trace dump failed trace_id=%s", self.trace_id)
            return None


def start_trace(trace_id: str, user_id: int, thread_id: str, input_meta: dict | None = None,
                dir: str | None = None, name: str | None = None, by_day: bool = True):
    """Register the trace in request context before submitting owned producer jobs.

    Evaluation can override storage and naming without mixing production artifacts.
    """
    rec = _TraceRecorder(trace_id, user_id, thread_id, input_meta or {},
                         trace_dir=dir, name=name, by_day=by_day)
    _recorder.set(rec)
    with _LOCK:
        _ACTIVE[trace_id] = rec
    return rec


def record(node: str, event: str, **data) -> None:
    """Append an event to the current request, including its final usage snapshot."""
    rec = _recorder.get()
    if rec is not None:
        rec.record(node, event, **data)


def set_reply(reply: str) -> None:
    """记录最终回复正文（producer 线程收尾时调用；空回复跳过）。"""
    rec = _recorder.get()
    if rec is not None:
        rec.set_reply(reply)


def finish_trace(trace_id: str, end_reason: str, duration_s: float, frames: int = 0) -> str | None:
    """Publish one completed recorder; production calls this after jobs drain.

    Disconnects and timeouts retain the recorder until the producer actually exits.
    Returns the published path, or None when absent, already finalized or unwritable.
    """
    rec = _ACTIVE.pop(trace_id, None)
    if rec is None:
        return None
    rec.finalize(end_reason, duration_s, frames)
    return rec.dump()
