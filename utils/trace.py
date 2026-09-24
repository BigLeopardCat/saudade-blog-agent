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

# 工具返回写进 trace 时留多长（字符）。**生产默认 200**：trace 是"节点事件序列"，
# 不是工具输出的第二份存档，全量留会把单份 trace 从几 KB 撑到几百 KB。
#
# 20260925 起可用 TRACE_TOOL_RESULT_LIMIT 覆盖，**唯一消费者是 L2 golden 轮**
# （`run_golden.run_case` 把它设成 8000）：评测侧的 LLM 评审员
# （`eval/llm_judge.py`）判"回复有没有编材料"时，**材料就是这里写下的东西**——
# 只留 200 字符，判官会理直气壮地把"文章里确实有、只是没记进 trace"的事实判成编造
# （实测：`rag_git_branch` 的 `get_article_detail` 只留了 200 字符，判官据此断定回复
# 编了「第 3.3 节」）。**生产不设这个变量** ⇒ 行为与改动前逐字一致。
# 运行时读环境（不缓存到模块级常量）：跑法在进程内改它也能生效，不依赖 import 顺序。
TOOL_RESULT_LIMIT_ENV = "TRACE_TOOL_RESULT_LIMIT"
TOOL_RESULT_LIMIT_DEFAULT = 200


def tool_result_text(result: str, tool: str = "") -> str:
    """工具返回 → 写进 trace 的文本。`rag_search` 例外：**一直全文**。

    （20260831 事故复盘：检索候选要能事后完整分析，"首位为什么是它"不该靠重跑复现。
    20260925 起其余工具在 golden 轮同样放开，理由见 TOOL_RESULT_LIMIT_ENV 注释。）

    限值语义：`正数` = 留这么多字符；`≤0` = 不截断（全文）；值写坏了 = 默认 200。
    """
    text = str(result)
    if tool == "rag_search":
        return text
    try:
        limit = int(os.environ.get(TOOL_RESULT_LIMIT_ENV) or TOOL_RESULT_LIMIT_DEFAULT)
    except ValueError:                       # 值写坏了 ⇒ 退回默认，**不是**放开
        logger.warning("[trace] %s 不是整数，工具返回按默认 %d 字符落盘",
                       TOOL_RESULT_LIMIT_ENV, TOOL_RESULT_LIMIT_DEFAULT)
        limit = TOOL_RESULT_LIMIT_DEFAULT
    return text if limit <= 0 else text[:limit]


class _TraceRecorder:
    def __init__(self, trace_id: str, user_id: int, thread_id: str, input_meta: dict,
                 trace_dir: str | None = None, name: str | None = None):
        self.trace_id = trace_id
        self.user_id = user_id
        self.thread_id = thread_id
        # 落盘目录与文件名可覆盖（20260922，golden set 用）：**默认仍是生产
        # trace 目录**——只有显式传参才改，误配的代价是把评测流量混进生产语料
        # （trace_alert/trace_metrics/效率基线扫的就是那个目录）。
        # `name` 给定时文件名就是 `<name>.json`（golden 要"一个用例一份、可直接点名"），
        # 否则沿用"时间戳_uid_trace_id 前 8 位"的可读命名。
        self.trace_dir = trace_dir or TRACE_DIR
        self.name = name or ""
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
            os.makedirs(self.trace_dir, exist_ok=True)
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
            # 20260830：文件名可读化——时间戳 + user_id + trace_id 前 8 位，
            # ls 目录即知哪次对话（纯 hash 命名要挨个点开才知道）；trace_id
            # 完整保留在 JSON 内对账。logrotate 按 traces/*.json 通配轮转（20260911
            # 起 rename+compress：源文件归档为 .1.gz 不复存在，读取端需支持 gz）
            stamp = self.started_at.replace("-", "").replace(":", "")
            if self.name:
                fname = f"{self.name}.json"
            else:
                fname = f"{stamp}_{self.user_id}_{self.trace_id[:8]}.json"
            path = os.path.join(self.trace_dir, fname)
            tmp = path + ".tmp"  # 原子替换：reader 不会读到半截文件
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(doc, f, ensure_ascii=False, indent=1)
            os.replace(tmp, path)
            return path
        except Exception:
            logger.exception("trace dump failed trace_id=%s", self.trace_id)
            return None


def start_trace(trace_id: str, user_id: int, thread_id: str, input_meta: dict | None = None,
                dir: str | None = None, name: str | None = None):
    """请求开始：创建 recorder，挂 contextvar（producer 线程可见）+ 全局注册表。

    在 chat_stream 任务里调用（middleware 已 set trace_id 的同一上下文）；
    producer 经 _submit_with_context 的 copy_context 继承，节点内 record 命中。

    `dir`/`name` 覆盖落盘位置与文件名（20260922 起 golden set 用；生产调用点
    一个都不传，行为与之前逐字一致）——**必须在"提交给线程池之前"的上下文里调**，
    否则 recorder 不会随 copy_context 传进 producer 线程（事件为空）。
    """
    rec = _TraceRecorder(trace_id, user_id, thread_id, input_meta or {},
                         trace_dir=dir, name=name)
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


def finish_trace(trace_id: str, end_reason: str, duration_s: float, frames: int = 0) -> str | None:
    """请求收尾：补收尾元数据并落盘（event_stream finally，所有退出路径）。

    任何退出路径（断连/超时/异常/正常收尾）都会走到——超时场景在 finally
    落盘中途 trace，事件序列里的最后一条即挂点。

    返回落盘路径（写失败/无此 recorder → None）。生产调用点忽略返回值，
    golden 用它把"哪条用例 → 哪份 trace"写进报告。
    """
    rec = _ACTIVE.pop(trace_id, None)
    if rec is None:
        return None
    rec.finalize(end_reason, duration_s, frames)
    return rec.dump()
