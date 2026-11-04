"""Centralised logging configuration."""

import contextvars
import logging
import re
import sys
import uuid

from config import settings

# Request jobs propagate this context into their owned worker threads.
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
            fmt=("%(asctime)s | %(name)-24s | %(levelname)-7s | "
                 "service=saudade-agent | pid=%(process)d | request_id=%(trace_id)s | %(message)s"),
            datefmt="%Y-%m-%dT%H:%M:%S%z",
        )
    )

    root = logging.getLogger()
    root.setLevel(level)
    # Replace existing handlers so repeated startup never duplicates a record.
    root.handlers = [handler]

    # Silence noisy third-party loggers
    for noisy in ("httpx", "httpcore", "urllib3", "openai"):
        logging.getLogger(noisy).setLevel(max(level, logging.WARNING))


def request_id(value: str | None) -> str:
    """Accept a bounded correlation value; never put untrusted control text in logs or paths."""
    if value and re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value):
        return value
    return uuid.uuid4().hex
