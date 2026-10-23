"""Serialize a conversation's read/modify/write cycle, including cancellations.

PostgreSQL advisory locks span workers. They use a separate autocommit connection:
holding a checkpoint-pool slot while waiting or calling an LLM can exhaust the
pool needed to finish the turn. No database transaction spans a model request.
In-memory savers use local locks; their entries disappear after the last waiter.
"""

import hashlib
import threading
import time
from contextlib import contextmanager

from agent.runtime import checkpoint_store

_registry_lock = threading.Lock()
_local_locks: dict[tuple, tuple] = {}
_POLL_SECONDS = 0.05


class AgentCancelled(Exception):
    """The caller stopped this turn; producers finish without emitting a reply."""


def stopped(config):
    event = ((config or {}).get("configurable") or {}).get("stop_event")
    return event is not None and event.is_set()


def check_cancelled(config):
    if stopped(config):
        raise AgentCancelled()


def _wait(config, deadline):
    check_cancelled(config)
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("同一会话仍有请求在处理，等待超时，请稍后重试")
    event = ((config or {}).get("configurable") or {}).get("stop_event")
    delay = min(_POLL_SECONDS, remaining)
    if event is None:
        time.sleep(delay)
    else:
        event.wait(delay)


@contextmanager
def _local_turn(saver, thread_id, config, deadline):
    key = (id(saver), thread_id)
    with _registry_lock:
        lock, users = _local_locks.get(key, (threading.Lock(), 0))
        _local_locks[key] = (lock, users + 1)
    acquired = False
    try:
        while not acquired:
            check_cancelled(config)
            acquired = lock.acquire(blocking=False)
            if not acquired:
                _wait(config, deadline)
        yield
    finally:
        if acquired:
            lock.release()
        with _registry_lock:
            _, users = _local_locks[key]
            if users == 1:
                del _local_locks[key]
            else:
                _local_locks[key] = (lock, users - 1)


@contextmanager
def _postgres_turn(saver, thread_id, config, deadline):
    import psycopg

    conninfo = getattr(saver.conn, "conninfo", None)
    if not conninfo:
        raise RuntimeError("PostgreSQL 会话锁要求 saver 使用带 conninfo 的连接池")
    key = int.from_bytes(hashlib.blake2b(
        ("saudade.hybrid.turn:" + thread_id).encode(), digest_size=8).digest(),
        "big", signed=True)
    with psycopg.connect(conninfo, autocommit=True, connect_timeout=5) as conn:
        acquired = False
        try:
            while not acquired:
                check_cancelled(config)
                acquired = conn.execute("SELECT pg_try_advisory_lock(%s)", (key,)).fetchone()[0]
                if not acquired:
                    _wait(config, deadline)
            yield
        finally:
            if acquired:
                conn.execute("SELECT pg_advisory_unlock(%s)", (key,))


@contextmanager
def conversation_turn(saver, config, *, timeout=60):
    check_cancelled(config)
    thread_id = str(((config or {}).get("configurable") or {}).get("thread_id") or "")
    if saver is None or not thread_id:
        yield
        return
    deadline = time.monotonic() + timeout
    is_postgres = type(saver).__module__.startswith("langgraph.checkpoint.postgres")
    guard = _postgres_turn if is_postgres else _local_turn
    with guard(saver, thread_id, config, deadline):
        if not ((config or {}).get("configurable") or {}).get("allow_deleted_thread"):
            checkpoint_store.require_active(saver, thread_id)
        yield
