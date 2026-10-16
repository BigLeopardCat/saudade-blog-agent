# -*- coding: utf-8 -*-
"""写操作幂等账的 **PG 后端**（`agent/idem.py::Ledger` 的同类实现，表在 saver 那个库里）。

## 为什么要有第二份实现

`agent/idem.py` 的账本本体是 agent 自持的一个 SQLite 文件（`settings.idem_db`）。那是
**临时做法**，主人已明确要迁到 PG。迁的方式不是"把 SQLite 换掉"，而是**加一档**：

  · 挂的是 PG saver（`PostgresSaver`，池上带 `.conn`）⇒ 账落**同一个池、同一个库**；
  · 其余（内存 saver / 没挂 saver / CI）⇒ 维持今天的 SQLite，逐字节不变。

这样选的理由是**保留期**：账与 checkpoint 必须一起裁——分开清会把本批要堵的那个洞
重新打开（清了账、留着 checkpoint ⇒ 恢复重放时账是空的 ⇒ 写落第二遍）。落在同一个库
里，这件事就变成一句"一起裁"，而不是两条要人工同步的运维规则。

后端选择写在 `agent/idem.py::_shared(config)`；本模块**只在真的要用它时**才被 import
（psycopg 不进 `idem.py` 的依赖面，`tests/test_write_idempotency.py` 块⑪ 钉这条）。

## 两个必须写下来的坑

① **行必须构造成 4 元组。** 池的 `row_factory` 是 `dict_row`（langgraph 要的）。直接把
   dict 交给 `idem._rehydrate` 的 `_status, text, kind, meta_json = row` 会**按键名解包**
   ⇒ 重放回下游的是字面串 `"text"`、`kind == "kind"`，**而且不报错**——它看起来像一次
   正常重放。所以本模块的每一次查询都显式 `cursor(row_factory=tuple_row)`。

② **`meta` 是 `text` 不是 `jsonb`。** SQLite 那格也是 TEXT，`_rehydrate` 拿到的是一段
   JSON 串；`jsonb` 列会让 psycopg 直接返回 dict ⇒ `json.loads(dict)` 抛 TypeError ⇒
   被 `except (TypeError, ValueError)` **静默吞掉** ⇒ meta 变 `{}` ⇒ 重放出来的命令帧
   被 `cmd_shape` 判 BLOCK（一次**凭空**的失败）。列类型是判据的一部分，不是口味。

## 为什么要求 autocommit

`ensure_autocommit` 的理由**不是**"不 autocommit 就不提交"——实测 `psycopg_pool` 里
`with conn:` 成功路径会走 `psycopg.Connection.__exit__` 的 `commit()`，非 autocommit
**也会**提交。真实差别是**提交点**：autocommit 让占位在**语句返回时**就落库，非
autocommit 的提交点在**块退出**。而"先记账再动手"这条协议要求占位在**远端调用之前**
真的落盘——硬杀落在任何位置都不能丢。顺带它还堵住"将来有人把事务跨过 `tool.invoke`
持有"这条缝。所以池里 `autocommit=True` 是**硬要求**，这里响亮地断言它。
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
    """池必须是 `autocommit=True`。假 ⇒ **响亮抛**（理由见模块头注）。

    读的是 `pool.kwargs`（psycopg_pool 把构造参数原样留在那儿）。读不到也算不通过——
    "看不出它是自动提交"与"它不是自动提交"在这里的后果一样：占位可能没在调用前落盘。
    """
    kwargs = getattr(pool, "kwargs", None)
    ok = isinstance(kwargs, dict) and kwargs.get("autocommit") is True
    if not ok:
        raise RuntimeError(
            f"幂等账要求连接池 autocommit=True，当前 kwargs={kwargs!r}。"
            "占位必须在**远端调用之前**按语句落库：非自动提交时提交点在块退出，"
            "硬杀落在块中间就丢，账本会静默失效（写落第二遍）。"
            "建池照 `agent/checkpoint_arm.py::_make_pool`。")


def setup(pool) -> None:
    """幂等建表（与 `saver.setup()` 同办）⇒ **不需要产线迁移脚本**。"""
    ensure_autocommit(pool)
    with pool.connection() as conn:
        conn.execute(_DDL)


class PostgresLedger:
    """一本 PG 账。`key` 是主键 ⇒ 多个 worker（生产 4 个）天然互斥。

    与 `idem.Ledger` 同协议（`claim` / `finish` / `close`），形状刻意保持一致——两档
    的读数要能直接对比（`checkpoint_probe.py --backend-parity` 就是这么比的）。

    池是**外部传入**的：账本不拥有它，所以 `close()` 是空转（关掉它等于把 saver 也关了）。
    """

    def __init__(self, pool):
        self.pool = pool

    def claim(self, key: str, tool: str, fp: str) -> tuple[str, tuple | None]:
        """占位。返回 `("new"|"done"|"intent", 行或 None)`。

        `INSERT ... ON CONFLICT DO NOTHING RETURNING key` + 回读是**一次原子判定**，
        原子性由主键保证：插进去 ⇒ 这一件从没做过；没插进去 ⇒ 行已存在，看它是
        `done`（重放）还是 `intent`（上一次死在调用中间 ⇒ 结果未知）。

        查询一律走 `tuple_row`（见头注①）：池自己的 `dict_row` 会让重放**静默**回错东西。
        """
        ensure_autocommit(self.pool)
        with self.pool.connection() as conn:
            with conn.cursor(row_factory=tuple_row) as cur:
                cur.execute(
                    f"INSERT INTO {TABLE}(key, tool, fp, status)"
                    " VALUES (%s, %s, %s, 'intent')"
                    " ON CONFLICT (key) DO NOTHING RETURNING key",
                    (key, tool, fp))
                if cur.fetchone() is not None:
                    return "new", None
                cur.execute(
                    f"SELECT status, text, kind, meta FROM {TABLE} WHERE key=%s",
                    (key,))
                row = cur.fetchone()
        if row is None:
            # 插入被忽略（行存在）却回读不到（行没了）——只可能是并发清理撞上了。
            # **不猜**：抛出去，交给上层 fail-closed（拒绝这次写）。
            raise RuntimeError("幂等账行状态不可判定（插入被忽略但回读为空）")
        if row[0] not in ("done", "intent"):
            raise RuntimeError(f"幂等账行状态认不出：{row[0]!r}")
        return row[0], row

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
