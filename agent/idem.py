"""Persist write intent and replay verified tool results.

The runtime binds each operation key to the signed user, conversation, operation ID,
tool and arguments. Production uses PostgresLedger through writes.ledger_for; SQLite
Ledger is an explicitly supplied local test fixture, never a production fallback.
An unfinished intent has an unknown outcome and must not be retried automatically.
Keep ledger retention aligned with retained checkpoints and confirmation records."""

import json
import os
import sqlite3
import threading
import time

from tools.base import ToolResult
from tools.result import error_frame

REASON_OUTCOME_UNKNOWN = "write_outcome_unknown"


# Oversized text is not retained; replay reports the missing body explicitly.
_RESULT_MAX = 20000


# A completed operation with an oversized result keeps a truthful replay placeholder.
_NOREPLAY_FRAME = ToolResult(
    "（这一步已经执行过：重放命中幂等账。原始返回超过留档上限，未留存，"
    "如需确切结果请重新读一次）", "ok", {})


def unknown_frame(name: str) -> ToolResult:
    """Report an unfinished write intent without granting permission to retry."""
    return error_frame(f"__ERROR__: 写操作结果未知[{REASON_OUTCOME_UNKNOWN}]"
            f"（{name} 上一次已经发出，进程在拿到返回之前中断了——**不要重试**，"
            f"重试可能把它做第二遍。先读一次确认它到底成没成，再把结果如实告诉主人）",
            REASON_OUTCOME_UNKNOWN)


def replay_result(row) -> str:
    """Restore persisted text, result kind and metadata; absent text uses a truthful fallback."""
    _status, text, kind, meta_json = row
    if text is None:
        return _NOREPLAY_FRAME
    try:
        meta = json.loads(meta_json) if meta_json else {}
    except (TypeError, ValueError):
        meta = {}
    return ToolResult(text, kind or "ok", meta if isinstance(meta, dict) else {})


# 取行的 SQL **只此一处**：多取一列 `fp`（本模块自己比对 `attempt` 用），比对完就由
# `_split_row` 剥掉。两个后端各写一遍列清单是它们各自方言决定的（`?` / `%s`），但**列序
# 与列数必须是同一份**——否则 `_split_row` 的下标会在一档上错位。
_SELECT_COLS = "status, fp, text, kind, meta"


def _split_row(row) -> tuple[str, tuple | None]:
    """Validate a ledger row and remove the attempt column before result replay."""
    if row is None:
        # 插入被忽略（行存在）却回读不到（行没了）——只可能是并发清理撞上了。
        # **不猜**：抛出去，交给上层 fail-closed（拒绝这次写）。
        raise RuntimeError("幂等账行状态不可判定（插入被忽略但回读为空）")
    if row[0] not in ("done", "intent"):
        raise RuntimeError(f"幂等账行状态认不出：{row[0]!r}")
    return row[0], (row[0], row[2], row[3], row[4])


class Ledger:
    """SQLite execution ledger used by explicitly supplied local tests.

    Connections are lazy and writes autocommit, so intent is durable before tool invocation."""

    def __init__(self, path: str):
        self.path = path
        self._lock = threading.Lock()
        self._conn = None

    # 冷启重试的退避与次数（见 `_connect` 的注）。20 × 50ms ≈ 1s 的上限。
    _OPEN_RETRIES = 20
    _OPEN_BACKOFF = 0.05

    def _connect(self):
        """Open the ledger and initialize its schema with bounded retries.

        SQLite WAL initialization may race across processes before the busy handler applies.
        Close failed connections and propagate exhausted retries to prevent untracked writes."""
        last: Exception | None = None
        for attempt in range(self._OPEN_RETRIES):
            if attempt:
                time.sleep(self._OPEN_BACKOFF)
            conn = sqlite3.connect(self.path, timeout=5.0, isolation_level=None)
            try:
                # `isolation_level=None` = 自动提交：`claim` 的 INSERT 必须在**发出
                # 请求之前**真的落盘，否则"先记账"这条协议是假的。
                conn.execute("PRAGMA busy_timeout=5000")
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute(
                    "CREATE TABLE IF NOT EXISTS idem ("
                    " key TEXT PRIMARY KEY, tool TEXT NOT NULL, fp TEXT NOT NULL,"
                    " status TEXT NOT NULL, ts REAL NOT NULL,"
                    " text TEXT, kind TEXT, meta TEXT)")
            except sqlite3.OperationalError as e:
                conn.close()          # 不关就是每重试一次漏一条连接
                last = e
                continue
            except Exception:
                conn.close()
                raise
            return conn
        raise last

    def _db(self):
        if self._conn is None:
            with self._lock:
                if self._conn is None:
                    parent = os.path.dirname(self.path)
                    if parent:
                        os.makedirs(parent, exist_ok=True)
                    self._conn = self._connect()
        return self._conn

    def claim(self, key: str, tool: str, attempt: str, *,
              resuming: bool = True) -> tuple[str, tuple | None]:
        """Atomically claim an operation or return its completed/unfinished record.

        Attempt identity is stored in fp. Resuming preserves an existing record; a caller
        explicitly starting a different attempt may reset it through a conditional update."""
        conn = self._db()
        cur = conn.execute(
            "INSERT OR IGNORE INTO idem(key, tool, fp, status, ts)"
            " VALUES (?,?,?,'intent',?)", (key, tool, attempt, time.time()))
        if cur.rowcount == 1:
            return "new", None
        row = conn.execute(f"SELECT {_SELECT_COLS} FROM idem WHERE key=?", (key,)).fetchone()
        if row is None:
            raise RuntimeError("幂等账行状态不可判定（插入被忽略但回读为空）")
        if row[1] == attempt or resuming:
            return _split_row(row)
        # 合法的重复：**一次原子的 UPDATE** 才算抢到（`fp` 与 `status` 一起进 WHERE，
        # 抢不到就是有人同时在动这一行 ⇒ 回读一次再判，不猜）。
        got = conn.execute(
            "UPDATE idem SET status='intent', fp=?, text=NULL, kind=NULL, meta=NULL, ts=?"
            " WHERE key=? AND fp=?", (attempt, time.time(), key, row[1])).rowcount
        if got == 1:
            return "new", None
        return _split_row(conn.execute(
            f"SELECT {_SELECT_COLS} FROM idem WHERE key=?", (key,)).fetchone())

    def finish(self, key: str, out) -> bool:
        """记完成。返回"结果是否因超上限而未留档"（撞顶时上层补一条 trace）。"""
        text = str(out)
        capped = len(text) > _RESULT_MAX
        self._db().execute(
            "UPDATE idem SET status='done', text=?, kind=?, meta=? WHERE key=?",
            (None if capped else text, str(getattr(out, "kind", "ok") or "ok"),
             json.dumps(getattr(out, "meta", None) or {}, ensure_ascii=False), key))
        return capped

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None
