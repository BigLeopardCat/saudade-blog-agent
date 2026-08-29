"""Per-request execution trace recording（roadmap 步骤 2：trace 落盘）。

每轮对话落一份 JSON trace：输入摘要、节点事件序列（planner/model/tools/
reflector 的分段耗时与关键数据）、最终回复、退出原因、总耗时。与日志互补：
日志是排障的粗粒度时间线，trace 是机器可读的结构化回放——RAG 动工后
"检索拖慢了多少"这类回归问题直接在 trace 里读分段耗时即可判定。

机制（两个坑的对应设计）：
  - _recorder contextvar 定位当前请求的 recorder：与 utils.logging._trace_id
    同源传播——producer 线程靠 _submit_with_context 的 copy_context 快照，
    线程内 record() 拿得到实例。
  - finish 由 event_stream finally 调用（asyncio 主任务），而 producer 线程
    可能仍挂起（LLM 超时场景）——不能靠线程返回值，用进程内 _ACTIVE dict
    中转：event_stream 按 trace_id 取出 recorder 补收尾元数据后落盘。
  - 超时场景的增量：LLM 挂起时最后一条事件就是挂点（如 model llm_start 后
    无 llm_done）——这正是 trace 相对日志的核心价值（15:21 事故：等待时长
    完全不可见）。落盘后置 dumped，线程晚到的收尾事件丢弃（不补写已落盘
    文件，避免 reader 读到半写状态）。
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

# 与项目 logs/ 目录对齐（日志体系规范见 CLAUDE.md §2）；settings 可经
# SAUDADE_TRACE_DIR 环境变量覆盖
TRACE_DIR = settings.trace_dir


class _TraceRecorder:
    def __init__(self, trace_id: str, user_id: int, thread_id: str, input_meta: dict):
        self.trace_id = trace_id
        self.user_id = user_id
        self.thread_id = thread_id
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

    def dump(self) -> None:
        if self.dumped:
            return
        self.dumped = True
        try:
            os.makedirs(TRACE_DIR, exist_ok=True)
            doc = {
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
            path = os.path.join(TRACE_DIR, f"{self.trace_id}.json")
            tmp = path + ".tmp"  # 原子替换：reader 不会读到半截文件
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(doc, f, ensure_ascii=False, indent=1)
            os.replace(tmp, path)
        except Exception:
            logger.exception("trace dump failed trace_id=%s", self.trace_id)


def start_trace(trace_id: str, user_id: int, thread_id: str, input_meta: dict | None = None):
    """请求开始：创建 recorder，挂 contextvar（producer 线程可见）+ 全局注册表。

    在 chat_stream 任务里调用（middleware 已 set trace_id 的同一上下文）；
    producer 经 _submit_with_context 的 copy_context 继承，节点内 record 命中。
    """
    rec = _TraceRecorder(trace_id, user_id, thread_id, input_meta or {})
    _recorder.set(rec)
    with _LOCK:
        _ACTIVE[trace_id] = rec
    return rec


def record(node: str, event: str, **data) -> None:
    """当前请求的节点事件（producer 线程内调用；非流式请求静默跳过）。"""
    rec = _recorder.get()
    if rec is not None:
        rec.record(node, event, **data)


def set_reply(reply: str) -> None:
    """记录最终回复正文（producer 线程收尾时调用；空回复跳过）。"""
    rec = _recorder.get()
    if rec is not None:
        rec.set_reply(reply)


def finish_trace(trace_id: str, end_reason: str, duration_s: float, frames: int = 0) -> None:
    """请求收尾：补收尾元数据并落盘（event_stream finally，所有退出路径）。

    任何退出路径（断连/超时/异常/正常收尾）都会走到——超时场景在 finally
    落盘中途 trace，事件序列里的最后一条即挂点。
    """
    rec = _ACTIVE.pop(trace_id, None)
    if rec is None:
        return
    rec.finalize(end_reason, duration_s, frames)
    rec.dump()
