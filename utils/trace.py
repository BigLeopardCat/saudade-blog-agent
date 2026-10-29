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

TOOL_RESULT_LIMITS: dict[str, int] = {"get_article_detail": 8000}
NO_LIMIT_TOOLS = frozenset({"rag_search"})
TRUNCATION_MARK_PREFIX = "…[trace 截断：原文共 "

# Optional fields/events are additive. Increment for changed field semantics.
# Missing schema_version denotes older retained traces.
SCHEMA_VERSION = 1


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
        self.user_id = user_id
        self.thread_id = thread_id
        # 落盘目录与文件名可覆盖（20260922，golden set 用）：**默认仍是生产
        # trace 目录**——只有显式传参才改，误配的代价是把评测流量混进生产语料
        # （trace_alert/trace_metrics/效率基线扫的就是那个目录）。
        # `name` 给定时文件名就是 `<name>.json`（golden 要"一个用例一份、可直接点名"），
        # 否则沿用"时间戳_uid_trace_id 前 8 位"的可读命名。
        #
        # `by_day`（20260925）：生产 trace 落 `<dir>/<YYYYMMDD>/`，**golden 必须传 False**。
        # 平铺到 20260925 积了 863 个文件（用户 20260925 提的"trace 列表很长"），
        # 按天一层后 `ls` 是 26 个目录；保留治理（`eval/trace_retention.py`）也按天对齐。
        # golden 不能跟着分：`golden_traces/<run_id>/` 已经是它自己的分目录，再套一层
        # 会让 `llm_judge` 的 `glob("*.json")`、`golden_trace.prune` 和报告里的路径全落空。
        self.trace_dir = trace_dir or TRACE_DIR
        self.name = name or ""
        self.by_day = by_day
        self.started_at = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())
        self._t0 = time.monotonic()
        self.input_meta = input_meta
        self.events: list = []
        self.reply = ""
        self.end_reason = None
        self.duration_s = None
        self.frames = 0
        self.dumped = False

    def record(self, node: str, event: str, **data) -> None:
        if self.dumped:
            return
        # 相对请求开始的单调偏移秒——跨事件排序/分段耗时直接读差值
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
                # 放第一个：读一份 trace 第一眼就知道该用哪套假设（见 SCHEMA_VERSION 的注释）
                "schema_version": SCHEMA_VERSION,
                "trace_id": self.trace_id,
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
            # 20260830：文件名可读化——时间戳 + user_id + trace_id 前 8 位，
            # ls 目录即知哪次对话（纯 hash 命名要挨个点开才知道）；trace_id
            # 完整保留在 JSON 内对账。保留期由 `eval/trace_retention.py` 执行、
            # 不再走 logrotate（20260925 起，见该脚本头注），读取端因此要同时认
            # `.json` 与 gz（存量归档件是 logrotate 时代留下的 .1.gz）
            stamp = self.started_at.replace("-", "").replace(":", "")
            if self.name:
                fname = f"{self.name}.json"
            else:
                fname = f"{stamp}_{self.user_id}_{self.trace_id[:8]}.json"
            # 按天分目录（20260925）：读侧枚举在 `utils/trace_files.py::iter_trace_files`
            # ——那个模块是"哪些文件算 trace"的唯一实现，改布局必须两处一起改
            # （`tests/test_trace_retention.py` 有一次真实落盘的往返断言盯着）。
            out_dir = os.path.join(self.trace_dir, stamp[:8]) if self.by_day else self.trace_dir
            # mode 显式 0700：20260925 安全审计 A6 把 logs/ + logs/agent/ + logs/agent/traces/
            # 收紧到 0700（里面是访客对话正文），而这个按天目录是**新建**的——默认 0755。
            # 今天父目录 0700 已经挡住横向越权，所以不是当下的暴露面；但边界一旦按审计建议
            # 放宽到 0750，逐级目录里只要有一层 0755 就等于没挡（www-data 在 group ubuntu 里）。
            # 与其留一个"靠上一层的模式兜着"的耦合，不如让这一层自己就是对的。
            os.makedirs(out_dir, mode=0o700, exist_ok=True)
            path = os.path.join(out_dir, fname)
            tmp = path + ".tmp"  # 原子替换：reader 不会读到半截文件
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(doc, f, ensure_ascii=False, indent=1)
            os.replace(tmp, path)
            return path
        except Exception:
            logger.exception("trace dump failed trace_id=%s", self.trace_id)
            return None


def start_trace(trace_id: str, user_id: int, thread_id: str, input_meta: dict | None = None,
                dir: str | None = None, name: str | None = None, by_day: bool = True):
    """请求开始：创建 recorder，挂 contextvar（producer 线程可见）+ 全局注册表。

    在 chat_stream 任务里调用（middleware 已 set trace_id 的同一上下文）；
    producer 经 _submit_with_context 的 copy_context 继承，节点内 record 命中。

    `dir`/`name` 覆盖落盘位置与文件名（20260922 起 golden set 用；生产调用点
    一个都不传，行为与之前逐字一致）——**必须在"提交给线程池之前"的上下文里调**，
    否则 recorder 不会随 copy_context 传进 producer 线程（事件为空）。

    `by_day` 默认 True（生产按天分目录）；**golden 调用点显式传 False**——理由见
    `_TraceRecorder.__init__` 里那段（golden 自己已有 `<run_id>/` 一层）。
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


def events_of(trace_id: str) -> list:
    """这次请求**已记录**的事件序列（未落盘也可读；无此 trace_id ⇒ 空表）。

    给**评测侧**一个不依赖落盘的读口：golden 要在判据里断言"某一轮系统真的把
    `planner.ledger_frame` 摆上桌了/摆的是哪几个 id"（见 `eval/run_golden.py`
    的 `require_ledger_frame` 与 `require_card_targets_from_ledger`）。**读文件
    不能替代它**：trace 的 json 是 `finish_trace` 才写的，而断言发生在跑完之后、
    `--only` 单例跑法里也没有"照路径回读"的稳定前提；更要紧的是落盘那份**已经过
    `reply` / `tool_result_text` 的截断**，拿它当判据等于让判据跟着截断漂移。

    只读、不改 recorder 状态（调用方拿到的是**拷贝**，往里面塞东西不会污染落盘——
    浅拷贝足够：断言只读 `event`/`ids` 这类标量键，不往 `events[]` 的嵌套结构里写）。
    **只为评测而存在**：生产侧没有任何调用点，`tests/test_trace_io.py` 的源码锁盯着
    "生产节点不许调它"这条（能力存在 ≠ 该在生产路径上用它）。
    """
    rec = _ACTIVE.get(trace_id)
    return list(rec.events) if rec is not None else []


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
