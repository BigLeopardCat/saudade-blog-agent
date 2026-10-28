# -*- coding: utf-8 -*-
"""PostgreSQL write ledger: atomic intent claims and stored execution receipts.

The checkpoint factory initializes the table under its startup lock. Writes use
short autocommit statements; no transaction or pool slot spans a remote call.
The shared row codec retains the same replay semantics as the SQLite test ledger.
"""

import json
import logging

from psycopg.rows import tuple_row

from agent import idem

logger = logging.getLogger(__name__)

# PostgreSQL and explicit SQLite test fixtures use distinct table names.
TABLE = "agent_write_idem"

_DDL = f"""
CREATE TABLE IF NOT EXISTS {TABLE} (
  key    text        PRIMARY KEY,
  tool   text        NOT NULL,
  fp     text        NOT NULL,
  status text        NOT NULL,
  ts     timestamptz NOT NULL DEFAULT now(),
  text   text,
  kind   text,
  meta   text
)
"""


def _meta_json(out) -> str:
    """Serialize result metadata using the shared replay format."""
    return json.dumps(getattr(out, "meta", None) or {}, ensure_ascii=False)


def ensure_autocommit(pool) -> None:
    """Reject pools that cannot durably record intent before remote writes."""
    kwargs = getattr(pool, "kwargs", None)
    if not isinstance(kwargs, dict) or kwargs.get("autocommit") is not True:
        raise RuntimeError("幂等账要求连接池 autocommit=True；远端写入前必须提交意图记录。")


def setup(pool) -> None:
    """Initialize the write ledger for an explicitly supplied autocommit pool."""
    ensure_autocommit(pool)
    with pool.connection() as conn:
        initialize(conn)


def initialize(conn) -> None:
    """Initialize once at startup using the already acquired setup connection."""
    if not conn.autocommit:
        raise RuntimeError("幂等账初始化要求 autocommit=True")
    conn.execute(_DDL)


class PostgresLedger:
    """Production write ledger using short autocommit claims and result storage.

    The primary key coordinates workers. The supplied pool remains owned by application
    lifespan, so close() does not close checkpoint connections."""

    def __init__(self, pool):
        self.pool = pool

    def claim(self, key: str, tool: str, attempt: str, *,
              resuming: bool = True) -> tuple[str, tuple | None]:
        """Atomically claim a new intent or read the persisted outcome.

        Attempt identity is stored in fp. Resuming preserves the existing record; an explicitly
        new attempt may reset it through a conditional update. Tuple rows preserve codec order."""
        ensure_autocommit(self.pool)
        with self.pool.connection() as conn:
            with conn.cursor(row_factory=tuple_row) as cur:
                cur.execute(
                    f"INSERT INTO {TABLE}(key, tool, fp, status)"
                    " VALUES (%s, %s, %s, 'intent')"
                    " ON CONFLICT (key) DO NOTHING RETURNING key",
                    (key, tool, attempt))
                if cur.fetchone() is not None:
                    return "new", None
                row = self._read(cur, key)
                if row is not None and row[1] != attempt and not resuming:
                    # 合法的重复：**一次原子的 UPDATE** 才算抢到（`fp` 一起进 WHERE ⇒
                    # 抢不到就是有人同时在动这一行 ⇒ 回读一次再判，不猜）。
                    cur.execute(
                        f"UPDATE {TABLE} SET status='intent', fp=%s, text=NULL,"
                        " kind=NULL, meta=NULL, ts=now() WHERE key=%s AND fp=%s",
                        (attempt, key, row[1]))
                    if cur.rowcount == 1:
                        return "new", None
                    row = self._read(cur, key)
        return idem._split_row(row)

    @staticmethod
    def _read(cur, key: str):
        """Read the shared replay columns in their defined order."""
        cur.execute(f"SELECT {idem._SELECT_COLS} FROM {TABLE} WHERE key=%s", (key,))
        return cur.fetchone()

    def finish(self, key: str, out) -> bool:
        """Persist the result and report whether its text exceeded the shared replay limit."""
        text = str(out)
        capped = len(text) > idem._RESULT_MAX
        with self.pool.connection() as conn:
            conn.execute(
                f"UPDATE {TABLE} SET status='done', text=%s, kind=%s, meta=%s"
                " WHERE key=%s",
                (None if capped else text, str(getattr(out, "kind", "ok") or "ok"),
                 _meta_json(out), key))
        return capped

    def close(self) -> None:
        """The application owns the shared pool; ledger cleanup leaves it open."""
