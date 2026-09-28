"""Centralised logging configuration."""

import contextvars
import logging
import sys
from config import settings

# 当前请求的 trace_id（contextvar：asyncio 任务内自动继承；run_in_executor 提交
# 的线程任务默认不拷贝 context，须由 server.py 提交前 copy_context() 显式快照，
# 否则线程内读回默认值 "-"）。无值时为 "-"。
_trace_id: contextvars.ContextVar = contextvars.ContextVar("trace_id", default="-")


def set_trace_id(trace_id: str) -> None:
    """设置当前上下文（请求）的 trace_id。调用方负责在请求结束时清理（reset）。"""
    _trace_id.set(trace_id)


def get_trace_id() -> str:
    return _trace_id.get()


def reset_trace_id() -> None:
    _trace_id.set("-")


class _TraceIdFilter(logging.Filter):
    """把 contextvar 中的 trace_id 注入每条 log record。"""

    def filter(self, record: logging.LogRecord) -> bool:
        record.trace_id = _trace_id.get()
        return True


def setup_logging() -> None:
    """Configure the root logger with consistent formatting and level."""
    level = getattr(logging, settings.log_level.upper(), logging.INFO)

    handler = logging.StreamHandler(sys.stdout)
    handler.setLevel(level)
    handler.addFilter(_TraceIdFilter())
    handler.setFormatter(
        logging.Formatter(
            fmt="%(asctime)s | %(name)-24s | %(levelname)-7s | tid=%(trace_id)s | %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )

    root = logging.getLogger()
    root.setLevel(level)
    # **整体替换**、不是 addHandler 累加：本函数在 lifespan 里每个 worker 各调一次，
    # 重复调用（reload / 测试里连调）不许把同一行日志打印两三遍。
    # 这里**刻意**连 root 上原有的 handler 一起丢掉——本进程的日志只走本模块这一种
    # 格式：agent.log 是 stdout 追加，混进第二种格式（比如某个库自己 basicConfig 的
    # 裸 `%(message)s`）对排障的伤害比"少一个日志来源"大得多。要改这条决定，
    # 先看 tests/test_logging_setup.py ② 那条判据在锁什么。
    #
    # 历史坑（20260928）：这里原来写的是 `if not root.handlers: addHandler(handler)`
    # 紧跟一个 `for noisy ...: ... else: root.handlers = [handler]`——for/else 的 else
    # 在"没有 break"时**恒执行**，于是上面那个 if 形同虚设（每次调用都重置一遍），
    # 而读代码的人以为幂等靠的是 if。现在只有一条路径，不再有两种解释。
    root.handlers = [handler]

    # Silence noisy third-party loggers
    for noisy in ("httpx", "httpcore", "urllib3", "openai"):
        logging.getLogger(noisy).setLevel(max(level, logging.WARNING))
