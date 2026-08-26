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
    # Avoid duplicate handlers on reload
    if not root.handlers:
        root.addHandler(handler)

    # Silence noisy third-party loggers
    for noisy in ("httpx", "httpcore", "urllib3", "openai"):
        logging.getLogger(noisy).setLevel(max(level, logging.WARNING))
    else:
        root.handlers = [handler]
