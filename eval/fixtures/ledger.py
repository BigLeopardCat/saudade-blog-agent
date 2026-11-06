"""SQLite write ledger for isolated evaluation; excluded from production packages."""

import json
import os
import sqlite3
import threading
import time

from agent.idem import _RESULT_MAX, _split_row

_SELECT_BY_KEY = "SELECT status, fp, text, kind, meta FROM idem WHERE key=?"


class Ledger:
    """SQLite execution ledger used by explicitly supplied local tests.

    Connections are lazy and writes autocommit, so intent is durable before tool invocation."""

    def __init__(self, path: str):
        self.path = path
        self._lock = threading.Lock()
        self._conn = None

    # Bound concurrent WAL initialization retries to about one second.
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
                # Autocommit makes intent durable before the external operation.
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
        row = conn.execute(_SELECT_BY_KEY, (key,)).fetchone()
        if row is None:
            raise RuntimeError("幂等账行状态不可判定（插入被忽略但回读为空）")
        if row[1] == attempt or resuming:
            return _split_row(row)
        # Only a conditional update may claim a different explicitly authorized attempt.
        got = conn.execute(
            "UPDATE idem SET status='intent', fp=?, text=NULL, kind=NULL, meta=NULL, ts=?"
            " WHERE key=? AND fp=?", (attempt, time.time(), key, row[1])).rowcount
        if got == 1:
            return "new", None
        return _split_row(conn.execute(
            _SELECT_BY_KEY, (key,)).fetchone())

    def lookup(self, key: str):
        row = self._db().execute(
            _SELECT_BY_KEY, (key,)).fetchone()
        return _split_row(row) if row is not None else None

    def finish(self, key: str, out) -> bool:
        """记完成。返回"结果是否因超上限而未留档"（撞顶时上层补一条 trace）。"""
        text = str(out)
        capped = len(text) > _RESULT_MAX
        self._db().execute(
            "UPDATE idem SET status='done', text=?, kind=?, meta=? WHERE key=?",
            (None if capped else text, str(getattr(out, "kind", "ok") or "ok"),
             json.dumps(getattr(out, "meta", None) or {}, ensure_ascii=False), key))
        return capped

    def replace(self, key: str, previous: tuple, out) -> bool:
        """Advance a client lifecycle only while its exact persisted result is current."""
        text = str(out)
        cursor = self._db().execute(
            "UPDATE idem SET text=?, kind=?, meta=? WHERE key=? AND status='done'"
            " AND kind=? AND meta=?",
            (None if len(text) > _RESULT_MAX else text, out.kind,
             json.dumps(out.meta, ensure_ascii=False), key, previous[2], previous[3]))
        return cursor.rowcount == 1

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None
