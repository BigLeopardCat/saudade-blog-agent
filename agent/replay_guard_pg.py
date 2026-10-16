# -*- coding: utf-8 -*-
"""重放闸的 PostgreSQL 实现（20261016 批 2）——**psycopg 只住在这个文件里**。

## 为什么必须切出来（这不是风格，是硬约束）

`agent/graph.py` **顶层** import `agent.replay_guard`，而**生产 venv 里没有 psycopg**
（只有实验环境 `/home/ubuntu/exp/pydeps` 有）。所以纯件那一侧一个字节都不许碰它——
同一条先例见 `agent/checkpoint_arm.py`（"psycopg 只住在臂层"）。判据在
`tests/test_replay_guard.py` ⑨a：`replay_guard.py` 源码里出现 `psycopg` 就红。

**依赖不进 `pyproject.toml`**（与 `checkpoint_arm` 同办）：那是「采纳」那一步的事。
今天生产从不 import 本模块 ⇒ 生产行为零变化。

## 表：自建，不需要产线迁移

`CREATE TABLE IF NOT EXISTS`，与 `PostgresSaver.setup()` 同一先例。**为什么不复用
LangGraph 的 `BaseStore`**（ADR-0011 里写过的那个候选）：`BaseStore.put` 是 **upsert**，
**没有 compare-and-set** ⇒ 占位不原子。而同 thread 真并发是被证明存在过的
（`checkpoint_probe.py --concurrency`）。主键 + `ON CONFLICT DO NOTHING` 一条语句就原子。

## ⚠️ 池必须 autocommit（这一条要配反控，见下）

占位若落在一个**未提交的事务**里，进程被杀时它会被一起回滚 ⇒ 账白做，而且**看起来
完全正常**（表在、代码在、只有崩溃那一刻的读数不对）。所以 `make_guard()` 建池时
`autocommit=True`，并且**主动断言**它——`ensure_autocommit` 让"接错池子"当场炸，
而不是静默退化成"账只在进程活着的时候有效"。反控在探针里：
`checkpoint_probe.py --crash-resume --guard-unsafe` 用一条**不提交**的连接占位，
读数应当回到 2 次。

**测试与生产都不 import 本模块**（CI 里没有 psycopg、也没有实验库）。
"""
import logging

from psycopg.types.json import Jsonb

from agent import replay_guard as RG

logger = logging.getLogger(__name__)

#: 表名（探针 `--pg-stats` 与排障按它查）。
TABLE = "agent_write_replay"

_SETUP_SQL = f"""
CREATE TABLE IF NOT EXISTS {TABLE} (
  thread_id  text        NOT NULL,
  task_id    text        NOT NULL,
  spec_index integer     NOT NULL,
  signature  text        NOT NULL,
  claimed_at timestamptz NOT NULL DEFAULT now(),
  outcome    jsonb,
  PRIMARY KEY (thread_id, task_id, spec_index)
)
"""

# 占位：插进去就是我的（原子），插不进去说明这一件已经有主。
_CLAIM_SQL = (f"INSERT INTO {TABLE} (thread_id, task_id, spec_index, signature) "
              "VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING RETURNING claimed_at")
_LOOKUP_SQL = (f"SELECT signature, outcome, claimed_at FROM {TABLE} "
               "WHERE thread_id = %s AND task_id = %s AND spec_index = %s")
_RECORD_SQL = (f"UPDATE {TABLE} SET outcome = %s "
               "WHERE thread_id = %s AND task_id = %s AND spec_index = %s")


def _ts(v) -> str | None:
    return None if v is None else str(v)


def ensure_autocommit(pool) -> None:
    """池**必须** autocommit（见模块头注）。接错池子当场炸，绝不静默降级。"""
    kw = (getattr(pool, "kwargs", None) or {})
    if not kw.get("autocommit"):
        raise RuntimeError(
            "重放闸的池必须 autocommit=True：占位若留在未提交的事务里，"
            "进程被杀时会被回滚 ⇒ 账白做，而表面看不出来（见 replay_guard_pg 头注）")


class PostgresReplayGuard:
    """`RG.ReplayGuard` 协议的真实现。三条契约：原子占位 / 占位即时提交 / 签名不符就抛。"""

    def __init__(self, pool):
        ensure_autocommit(pool)
        self.pool = pool

    def claim(self, key, signature):
        thread, task, idx = str(key[0]), str(key[1]), int(key[2])
        with self.pool.connection() as conn:
            row = conn.execute(_CLAIM_SQL, (thread, task, idx, signature)).fetchone()
            if row is not None:
                return RG.Claim(proceed=True, claimed_at=_ts(row["claimed_at"]))
            # 没插进去 ⇒ 这一件已经有主：可能是「已做完」（回放结局），也可能是
            # 「发出去了、结果没记下来」（in-doubt）。**不许糊**，见 replay_guard 头注。
            cur = conn.execute(_LOOKUP_SQL, (thread, task, idx)).fetchone()
        if cur is None:
            # 理论到不了（刚才的 ON CONFLICT DO NOTHING 没插进去 = 行存在）。真到了
            # 说明有人在中间删了行 ⇒ 分不清第几次 ⇒ 响亮抛，绝不当作"没做过"继续。
            raise RG.ReplayUnavailable(f"{TABLE} 里 {(thread, task, idx)} 的行凭空消失了")
        if cur["signature"] != signature:
            raise RG.ReplayConflict(
                f"{(thread, task, idx)} 上记的是另一件 spec"
                f"（记的是 {cur['signature'][:80]}，现在是 {signature[:80]}）")
        outcome = cur["outcome"]
        return RG.Claim(proceed=False,
                        outcome=outcome if isinstance(outcome, dict) else None,
                        claimed_at=_ts(cur["claimed_at"]))

    def record(self, key, outcome):
        thread, task, idx = str(key[0]), str(key[1]), int(key[2])
        with self.pool.connection() as conn:
            conn.execute(_RECORD_SQL, (Jsonb(dict(outcome)), thread, task, idx))


def setup(pool) -> None:
    """建表（幂等）。与 `PostgresSaver.setup()` 同办：自建表 ⇒ **不需要产线迁移**。"""
    with pool.connection() as conn:
        conn.execute(_SETUP_SQL)
    logger.info("[replay] 账就绪：表 %s（同库同 DSN，自建、免迁移）", TABLE)
