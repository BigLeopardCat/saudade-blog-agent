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

# 工具返回写进 trace 时留多长（字符）。**上限是天花板不是配额**——返回短的工具一分钱
# 不多花，所以档位按"事后核查这条返回**需要**多少材料"定，不按"典型多长"定。
#
# 20260925 从「一律 200」改成分档，两个动机都有实证：
#   ① 材料被悄悄砍掉：判官（`eval/llm_judge.py`）判"回复有没有编材料"时，材料就是这里
#      写下的东西——只留 200 字符，它会把文章里确实有、只是没记进 trace 的事实判成编造
#      （实测 `rag_git_branch`：`get_article_detail` 只留 200 字符，判官据此断定回复编了
#      「第 3.3 节」）。
#   ② 截断**看不出来**：`text[:limit]` 不带任何标记，读 trace 的人和脚本只能靠"长度恰好
#      等于上限"这个启发式猜（`llm_judge.truncated_calls` 就是这么猜的）。现在任何截断
#      都带 TRUNCATION_MARK（含原文长度），猜测那半退化成兼容老 trace 的兜底。
#
# 档位数值来自 20260925 golden 那一批**不受限**的实测（641 份 trace 的 call 事件）：
#   get_article_detail 最大 40000（撞到 golden 的上限，是唯一撞的）、list_guestbook 4335、
#   list_talks 2254、list_notes 1107、list_tags 993、get_service_health 617，其余 ≤ 700。
# 于是：正文单列 8000（再长就该按小节读——`agent/sections.py` 机制既有），
# 默认 4000 覆盖除正文外的全部实测最大。体积代价：约 33 份/天、每份最坏几 KB
# ⇒ 一年 100MB 量级（20260925 实测：27 天 3.7MB；磁盘余 9.1G）。
#
# `TRACE_TOOL_RESULT_LIMIT` 仍然**全局**覆盖分档（不是"只改默认档"）：L2 golden 轮
# `run_golden.run_case` 把它设成 40000，判官要材料。**生产不设这个变量** ⇒ 走分档。
# 运行时读环境（不缓存到模块级常量）：跑法在进程内改它也能生效，不依赖 import 顺序。
TOOL_RESULT_LIMIT_ENV = "TRACE_TOOL_RESULT_LIMIT"
TOOL_RESULT_LIMIT_DEFAULT = 4000

# 单工具覆盖（名字 → 上限；0/负数 = 不截断）。改这里要同改 tests/test_trace_truncation.py
# 里那几条断言——它们是"这档是不是还在"的唯一机械证据。
TOOL_RESULT_LIMITS: dict[str, int] = {
    # 长正文：唯一会撞到上限的工具（golden 40000 档实测撞满）。8000 够核"回复引的那段在不在"
    "get_article_detail": 8000,
}

# 不截断的工具（一直是全文）。`rag_search` 是 20260831 事故复盘定的：检索候选要能事后完整
# 分析，"首位为什么是它"不该靠重跑复现。
NO_LIMIT_TOOLS = frozenset({"rag_search"})

# 截断标记（带原文长度）。机器判定用 `is_truncated()`，**别在别处硬编码这段文本**。
TRUNCATION_MARK_PREFIX = "…[trace 截断：原文共 "


def truncation_mark(original_len: int) -> str:
    return f"{TRUNCATION_MARK_PREFIX}{original_len} 字符]"


def is_truncated(text: str) -> bool:
    """这段落进 trace 的返回文本**被截断过**吗（标记说了算，不靠长度猜）。"""
    return TRUNCATION_MARK_PREFIX in str(text)


def tool_result_text(result: str, tool: str = "") -> str:
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
        limit = TOOL_RESULT_LIMITS.get(tool, TOOL_RESULT_LIMIT_DEFAULT)
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
            # 按天分目录（20260925）：读侧枚举在 `eval/trace_files.py::iter_trace_files`
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
