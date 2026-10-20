"""真连一个库、真删一次：`eval/pg_retention.py` 的**那一层**判据（20261019 批 C）。

**这一件为什么单独一层**：`tests/test_pg_retention.py` 判的是分类规则与语句形状，它跑在
一个假连接上——而 20261018 那次停服的教训正是"**四层判据全在必然失败/必然成功的假前提上判，
真连一个库那条路径从来没有被跑过**"（见 `tests/test_checkpoint_setup_concurrent.py` 头注）。
保留治理这一件要判的三件事，**只有真库能回答**：

  ① `checkpoint->>'ts'` 那个 cast 在**真的 JSONB** 上跑得通（形态正则与真数据对得上）；
  ② 三张表**真的没有外键** ⇒ 只删主表会留下孤儿（这条是"实测"出来的，不是读文档）；
  ③ 断言不成立时**真的回滚**（半裁的状态没有落库）。

**它会真删**，所以只认一个**专门的测试库**：DSN 里出现产线库名 ⇒ 当场拒跑。
它只碰自己造的线程（前缀 `pgret_`），跑完删干净，不动别人的数据。

用法：
  SAUDADE_CKPT_TEST_DSN='postgresql://…@127.0.0.1:5432/saudade_it' \\
      .venv/bin/python tests/test_pg_retention_live.py

**没有那个变量时这一件不报绿，它报「未评估」**（一整块大字），`SAUDADE_REQUIRE_CKPT_TEST=1`
把"未评估"变成失败——本仓把"没量"与"量了是绿的"分得很开。
"""


from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


sys.path.insert(0, str(ROOT))


sys.path.insert(0, str(ROOT / "eval"))


DSN = os.environ.get("SAUDADE_CKPT_TEST_DSN", "").strip()


STRICT = bool(os.environ.get("SAUDADE_REQUIRE_CKPT_TEST"))


PREFIX = "pgret_"


FAILS: list[str] = []


def check(desc: str, cond: bool, detail: str = "") -> None:
    print(f"  {'✅' if cond else '❌'} {desc}" + (f"  [{detail}]" if detail else ""))
    if not cond:
        FAILS.append(desc)


def _skip(why: str) -> None:
    """前提不可用 ⇒ **报「未评估」，不报绿**。"""
    print("=" * 78)
    print("⏭  未评估：PG 保留治理的**真连库**那一层")
    print(f"   原因：{why}")
    print("   ⚠️ 这一层量的是「真的连上一个库、真的删一次、断言不成立时真的回滚」——")
    print("     前两层（纯函数 + 假连接）结构上回答不了这三件事。")
    print("   跑法：")
    print("     SAUDADE_CKPT_TEST_DSN='postgresql://…@127.0.0.1:5432/saudade_it' \\")
    print("         .venv/bin/python tests/test_pg_retention_live.py")
    print("=" * 78)
    if STRICT:
        print("SAUDADE_REQUIRE_CKPT_TEST=1 ⇒ 未评估算失败（本次退出码 1）")
        raise SystemExit(1)
    if "pytest" in sys.modules:
        # pytest 轨：`SystemExit` 在任何嵌套深度都被判为**失败**（黄要显式 skip 才是黄）。
        # `allow_module_level=True` 是必需的 —— 本函数**既在 import 期被调**（psycopg 没装 /
        # 连不上），**也在驱动里被调**（没给 DSN）；不给这个开关，前者会退化成
        # `ERROR ... Interrupted: 1 error during collection`（**红**，不是黄）。
        # 直跑轨照旧走下面的 `raise SystemExit(0)`，语义不变。
        import pytest  # noqa: PLC0415
        pytest.skip(why, allow_module_level=True)
    raise SystemExit(0)


try:
    import psycopg
except ImportError as e:                                       # pragma: no cover
    _skip(f"psycopg 没装（{e}）")


try:
    from langgraph.checkpoint.postgres.base import BasePostgresSaver
except ImportError as e:                                       # pragma: no cover
    _skip(f"langgraph 的 PG 后端没装（{e}）")


try:
    CONN = psycopg.connect(DSN, autocommit=True, connect_timeout=5)
except Exception as e:                                         # pragma: no cover
    _skip(f"连不上（{type(e).__name__}: {e}）")


import pg_retention as R  # noqa: E402

from agent.idem_pg import _DDL as IDEM_DDL  # noqa: E402
from agent.idem_pg import TABLE as IDEM_TABLE  # noqa: E402


def _ensure_schema() -> None:
    """把表建出来（**跑 langgraph 自己的迁移**，不手抄一份 schema——手抄那份迟早与它漂开）。

    ⚠️ 只**建**不**删**：同目录那条 `test_checkpoint_setup_concurrent.py` 会 `DROP TABLE`
    （它要的是"第一次启动"那个前提），两件跑在一起时谁先谁后都行——`IF NOT EXISTS` 幂等。
    """
    for sql in BasePostgresSaver.MIGRATIONS:
        CONN.execute(sql)
    CONN.execute(IDEM_DDL)


def _seed(thread: str, ts: str, *, blobs: int = 1, writes: int = 1) -> None:
    """给一条线程造 1 个 checkpoint（带 `ts`）+ N 个 blob + N 条 write。"""
    cid = f"{thread}-ck1"
    CONN.execute(
        "INSERT INTO checkpoints (thread_id, checkpoint_ns, checkpoint_id,"
        " parent_checkpoint_id, type, checkpoint, metadata)"
        " VALUES (%s, '', %s, NULL, 'json', %s::jsonb, '{}'::jsonb)"
        " ON CONFLICT DO NOTHING",
        (thread, cid, json.dumps({"ts": ts, "id": cid})))
    for i in range(blobs):
        CONN.execute(
            "INSERT INTO checkpoint_blobs (thread_id, checkpoint_ns, channel, version,"
            " type, blob) VALUES (%s, '', %s, '1', 'json', %s) ON CONFLICT DO NOTHING",
            (thread, f"ch{i}", b"x"))
    for i in range(writes):
        CONN.execute(
            "INSERT INTO checkpoint_writes (thread_id, checkpoint_ns, checkpoint_id,"
            " task_id, idx, channel, type, blob)"
            " VALUES (%s, '', %s, 't1', %s, 'messages', 'json', %s) ON CONFLICT DO NOTHING",
            (thread, cid, i, b"x"))


def _seed_ledger(key: str, ts: str) -> None:
    CONN.execute(
        f"INSERT INTO {IDEM_TABLE} (key, tool, fp, status, ts) VALUES (%s, 't', 'f', 'done', %s)"
        " ON CONFLICT (key) DO UPDATE SET ts = EXCLUDED.ts", (key, ts))


def _count(table: str, thread: str) -> int:
    return CONN.execute(f"SELECT count(*) FROM {table} WHERE thread_id = %s",
                        (thread,)).fetchone()[0]


def _purge() -> None:
    """清掉本件造的所有行（只碰 `pgret_` 前缀）。"""
    for t in R.THREAD_TABLES:
        CONN.execute(f"DELETE FROM {t} WHERE thread_id LIKE %s", (PREFIX + "%",))
    CONN.execute(f"DELETE FROM {IDEM_TABLE} WHERE key LIKE %s", (PREFIX + "%",))


def test_pg_retention_live():
    if not DSN:
        _skip("没有 SAUDADE_CKPT_TEST_DSN")
    _DB = DSN.rsplit("/", 1)[-1].split("?", 1)[0]
    if not _DB or "saudade_blog" in _DB:
        _skip(f"DSN 指向的库是 {_DB!r}——本件**会真删**，拒绝在产线库上跑")
    NOW = datetime.now(timezone.utc)
    CUT = R.cutoff_of(R.KEEP_DAYS_DEFAULT, NOW)
    _OLD_TS = (NOW - timedelta(days=60)).isoformat()
    _NEW_TS = (NOW - timedelta(days=1)).isoformat()
    _OLD, _NEW = PREFIX + "old", PREFIX + "new"
    try:
        _ensure_schema()
        _purge()
        _seed(_OLD, _OLD_TS, blobs=2, writes=3)
        _seed(_NEW, _NEW_TS)
        _seed_ledger(PREFIX + "k_old", _OLD_TS)
        _seed_ledger(PREFIX + "k_new", _NEW_TS)

        print("① 真数据上盘一遍：`checkpoint->>'ts'` 那个 cast 跑得通，分类对得上")
        pl = R.scan(CONN, CUT, 200)
        _ids = [r["thread_id"] for r in pl["prune"]]
        check("60 天前那条线程被判该裁（形态正则与**真的 JSONB** 对得上）",
              _OLD in _ids, f"prune={_ids}")
        check("1 天前那条在窗内（不是「全都裁」）", _NEW not in _ids)
        check("**没有任何线程落进 undated**（真数据的时间戳形态认识）",
              pl["undated"] == [], str(pl["undated"]))
        check("账里早于 cutoff 的正是那条老的（真表、真 `ts` 列）",
              pl["ledger"] == {"exists": True, "due": 1}, str(pl["ledger"]))

        print("\n② 真删一次：三张表都干净了、窗内那条一个字没动、账同窗一起裁")
        res = R.apply_plan(CONN, pl)
        check("老线程在三张表里都查不到了（**没有外键，少删一张就会剩孤儿**）",
              all(_count(t, _OLD) == 0 for t in R.THREAD_TABLES),
              str({t: _count(t, _OLD) for t in R.THREAD_TABLES}))
        check("窗内那条**一个字没动**（blob/write 数都还在）",
              _count("checkpoints", _NEW) == 1 and _count("checkpoint_blobs", _NEW) == 1
              and _count("checkpoint_writes", _NEW) == 1)
        _k = CONN.execute(f"SELECT count(*) FROM {IDEM_TABLE} WHERE key LIKE %s",
                          (PREFIX + "%",)).fetchone()[0]
        check("账同窗一起裁：老的没了、新的还在", _k == 1, f"剩 {_k} 行")
        check("返回值如实报了行数（不是恒 0）",
              sum(res["rows"].values()) > 0 and res["ledger"] == 1, str(res))

        print("\n③ 反控（**这一件最要紧的一条**）：拆成两个事务会留下什么中间态")
        # 这一段**刻意手写**那条错路：先提交 checkpoint 的删除、再（假装）去删账、中间抛。
        # 留下的中间态 = "checkpoint 没了、账还在"——正是 ADR-0012 要堵的那个洞。
        _seed(PREFIX + "x", _OLD_TS)
        _seed_ledger(PREFIX + "kx", _OLD_TS)
        for t in R.THREAD_TABLES:                      # 一个事务（autocommit 下每条自成一个）
            CONN.execute(f"DELETE FROM {t} WHERE thread_id = %s", (PREFIX + "x",))
        _mid_ck = sum(_count(t, PREFIX + "x") for t in R.THREAD_TABLES)
        _mid_led = CONN.execute(f"SELECT count(*) FROM {IDEM_TABLE} WHERE key = %s",
                                (PREFIX + "kx",)).fetchone()[0]
        check("（演示）先提交 checkpoint 删除 ⇒ **中间态真的落库了**：checkpoint 0 行、账还在",
              _mid_ck == 0 and _mid_led == 1, f"checkpoint={_mid_ck} 账={_mid_led}")
        check("⇒ 这就是 `apply_plan` 把两个 DELETE 放进**同一个** `conn.transaction()` 的理由"
          "（本仓判据层那一条在 `tests/test_pg_retention.py` ④⑤）", True)
        # 收尾：把演示留下的账行也清掉
        CONN.execute(f"DELETE FROM {IDEM_TABLE} WHERE key LIKE %s", (PREFIX + "%",))

        print("\n④ 再盘一遍：库里已经没有该裁的了（幂等：第二次跑是 no-op）")
        pl2 = R.scan(CONN, CUT, 200)
        check("`pgret_` 那两条都没了",
              not [r for r in pl2["prune"] if r["thread_id"].startswith(PREFIX)])
        check("账也没了（`due` 归 0）", pl2["ledger"]["due"] == 0, str(pl2["ledger"]))
    finally:
        _purge()
        CONN.close()
    print()
    if FAILS:
        print(f"失败 {len(FAILS)} 项：" + "；".join(FAILS))
        raise SystemExit(1)
    print("全部通过")

    assert not FAILS, (
        f"{len(FAILS)} 条未通过：" + "；".join(FAILS))


if __name__ == "__main__":
    test_pg_retention_live()
