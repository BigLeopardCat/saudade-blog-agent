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

# 表名刻意与 SQLite 那格（`idem`）不同名：两档会同时存在（CI 走 SQLite、实验/产线走 PG），
# 同名只会让人把两张表看混。
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
    """`meta` 的落库形态：一段 JSON 串，与 SQLite 那格逐字同形（见头注②）。"""
    return json.dumps(getattr(out, "meta", None) or {}, ensure_ascii=False)


def ensure_autocommit(pool) -> None:
    """Reject pools that cannot durably record intent before remote writes."""
    kwargs = getattr(pool, "kwargs", None)
    if not isinstance(kwargs, dict) or kwargs.get("autocommit") is not True:
        raise RuntimeError("幂等账要求连接池 autocommit=True；远端写入前必须提交意图记录。")


def setup(pool) -> None:
    """幂等建表（与 `saver.setup()` 同办）⇒ **不需要产线迁移脚本**。"""
    ensure_autocommit(pool)
    with pool.connection() as conn:
        initialize(conn)


def initialize(conn) -> None:
    """Initialize once at startup using the already acquired setup connection."""
    if not conn.autocommit:
        raise RuntimeError("幂等账初始化要求 autocommit=True")
    conn.execute(_DDL)


class PostgresLedger:
    """一本 PG 账。`key` 是主键 ⇒ 多个 worker（生产 4 个）天然互斥。

    与 `idem.Ledger` 同协议（`claim` / `finish` / `close`），形状刻意保持一致——两档
    的读数要能直接对比（`checkpoint_probe.py --backend-parity` 就是这么比的）。

    池是**外部传入**的：账本不拥有它，所以 `close()` 是空转（关掉它等于把 saver 也关了）。
    """

    def __init__(self, pool):
        self.pool = pool

    def claim(self, key: str, tool: str, attempt: str, *,
              resuming: bool = True) -> tuple[str, tuple | None]:
        """占位。返回 `("new"|"done"|"intent", 行或 None)`。

        `INSERT ... ON CONFLICT DO NOTHING RETURNING key` + 回读是**一次原子判定**，
        原子性由主键保证：插进去 ⇒ 这一件从没做过；没插进去 ⇒ 行已存在，再按两格分流
        （口径与理由见 `agent/idem.py` 头注《修法》，两档**必须同一份语义**）：

          · `行.fp == attempt` ⇒ 同一 superstep 的重放 ⇒ 逐字就是今天的行为；
          · 换了 attempt 而 `resuming` ⇒ 上一轮没跑完 ⇒ 那一行就是那一轮的尝试；
          · 换了 attempt 而非 `resuming` ⇒ 主人明确再要一次 ⇒ 原子地改回 `intent`。

        `attempt` 存在 `fp` 列里（**列名与表结构不变**）。查询一律走 `tuple_row`
        （见头注①）：池自己的 `dict_row` 会让重放**静默**回错东西。
        """
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
        """回读一行（列清单与 SQLite 那档**同一份**，见 `idem._SELECT_COLS`）。"""
        cur.execute(f"SELECT {idem._SELECT_COLS} FROM {TABLE} WHERE key=%s", (key,))
        return cur.fetchone()

    def finish(self, key: str, out) -> bool:
        """记完成。返回"结果是否因超上限而未留档"（撞顶时上层补一条 trace）。

        上限与 SQLite 那格**共用同一个** `idem._RESULT_MAX`：两档对"多大的返回值值得
        留"必须给同一个答案，否则同一个写走哪一档会得到不同的重放结果。
        """
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
        """空转：池是外部传入的（关掉它等于把 saver 一起关了）。"""
