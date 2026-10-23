#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""checkpoint store 与幂等账的**保留治理**（PG 那一路）：默认只列不删，`--apply` 才动手。

## 为什么需要它（20261019 批 C）

`eval/retention_manifest.py` 那张登记表**只审计盘上的树**（`logs/` 与 `eval/report/`），
而 20261018 采纳之后 agent 的上下文落进了 PG —— 那张表对它是瞎的：

  · `checkpoints` 那一族**每个 checkpoint 存整份快照**（实测：`messages` 通道的 blob
    随会话长度 1417 → 2917 → 2441 → 3713 → 5135 → 6646 字节单调长），而一条会话的
    checkpoint 数随轮数线性长 ⇒ **存储按会话长度平方增长**，且**没有任何东西在裁它**；
  · 幂等账 `agent_write_idem` 同样零清理（它的登记项写在 `logs/` 那条降级档上，
    理由里那句"与将来的 checkpoint store 必须一起设计"就是本文件）。

同族的先例是 `eval/trace_retention.py`（logrotate 的 `rotate 14` 对"文件名唯一"的产物
结构上无效）与 R2 的 `--keep 3`：**保留策略必须由一个真的会跑的东西执行**，写在配置或
登记表里不算。本文件就是那个执行者，接在 `scripts/nightly_regression.sh` 末位。

## 两条不变量（这一件真正的判据，比"删掉多少行"重要）

**① 账与 checkpoint 必须一起裁。** 留下 checkpoint 却删掉账本行，等于把幂等账要堵的那个
洞重新打开（ADR-0012）。所以：**同一个 cutoff、同一个事务、commit 前断言**。断言不成立
⇒ 整个事务回滚（宁可什么都不删，也不留下半裁的状态）。

**② 取不到时间的线程永不裁。** 线程的"最近活动"取自 `max(checkpoint->>'ts')`——
`checkpoints` **没有时间列**（DDL 里只有 `thread_id / checkpoint_ns / checkpoint_id /
parent_checkpoint_id / type / checkpoint / metadata`），时间只住在 `checkpoint` 那个 JSONB
里。取不到（NULL）就是"这条线程的年龄不可知" ⇒ **不动它**（判据缺失时不动手，同
`trace_retention` 那条"先看清再删"）。

⚠️ **如实的残余（别把这一件读成"账与 checkpoint 逐线程一一对应"）**：`agent_write_idem`
**没有 `thread_id` 列**（键是 `sha256(...)`，见 `agent/idem.py`），所以无法逐线程对齐。
本文件的口径是"**同窗**"而不是"**同一批线程**"：账按 `ts < cutoff` 全裁，checkpoint 只裁
**最近活动早于 cutoff 的那些线程**。⇒ 一条长期活跃的会话，它 30 天前的 checkpoint 会留下、
对应的账行会被删掉。**这个方向是安全的**：重放只针对线程的**最新** checkpoint
（`langgraph/pregel/_loop.py` 的恢复语义），而活跃线程的最新 checkpoint 必然在窗内 ⇒
早于 N 天的账行**永远等不到它的读者**。要结构性保证就得给账表加 `thread_id` 列
（`idem_pg.setup()` 的 DDL + `claim` 时写 `configurable.thread_id`），那是后续一件独立的事。

## 与库无关

DSN 从 `--dsn` 或 `CHECKPOINT_DSN` 读，**不硬编码库名**（本仓是**公开仓**，运维坐标不落仓；
同族纪律见 `agent/checkpoint.py` 头注《DSN 指向哪个库》）。表名写在这里是因为**它们是
langgraph 的公开 schema**（`BasePostgresSaver.MIGRATIONS` 里逐条写着），不是某台机器的坐标。

## 跑法（cd saudade-blog-agent）

  .venv/bin/python eval/pg_retention.py                    # 只列不删（默认）
  .venv/bin/python eval/pg_retention.py --json             # 机器可读摘要
  .venv/bin/python eval/pg_retention.py --apply            # 真删
  .venv/bin/python eval/pg_retention.py --keep-days 7 --apply
  .venv/bin/python eval/pg_retention.py --dsn 'postgresql://…'   # 换库（离线测试用）

退出码：0 = 跑完了（含"没有可删的"与"这个库还没建表"）；1 = 有错（连不上 / 断言不成立 /
删除失败）。**"没配 DSN"退 0 并印一行响亮的"这一层没量"**——它和"跑过了、没有可删的"
是两件事，别让汇总里那行 ✓ 把它们混成一个（同离线套件那条「未评估」口径）。
"""
import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone

# 保留期**唯一的数在这里**（夜间脚本只传 --apply，不复述这个数）。
# 与 `eval/trace_retention.py::KEEP_DAYS_DEFAULT` 同量级，且**远大于**崩溃恢复窗口
# （那个窗口是秒级：账行的意义只到"同一个 superstep 的恢复"为止）。
KEEP_DAYS_DEFAULT = 30
FENCE_KEEP_DAYS = 30

# 一次最多裁多少条线程（**必须有上限**：第一次跑可能面对几万条陈线程，
# 一条 DELETE 全干完会把库锁住很久，而这是夜间任务、不需要一口气做完）。
# 超预算时只裁**最老的**那一批（按最近活动时间升序）。
MAX_THREADS_DEFAULT = 200

# langgraph 的公开 schema（`BasePostgresSaver.MIGRATIONS` 逐条 CREATE 的就是这三张）。
# ⚠️ **三张都要删，不能只删 `checkpoints`**：实测这个版本的三张表之间**一条外键都没有**
# （`pg_constraint` 里 `conrelid LIKE 'checkpoint%'` 查出来是空的）⇒ 只删主表会留下
# 孤儿 blob/writes，而它们照样占空间、且没有任何读取路径会再碰它们。
# `checkpoint_migrations` **不在这个表里**：它记的是"这套表建到第几版"，是 schema 账不是数据。
THREAD_TABLES = ("checkpoints", "checkpoint_blobs", "checkpoint_writes")

# 幂等账（`agent/idem_pg.py` 的 DDL）。时间列叫 `ts`（`timestamptz NOT NULL DEFAULT now()`）。
LEDGER_TABLE = "agent_write_idem"
LEDGER_TS = "ts"
FENCE_TABLE = "agent_deleted_threads"

# `checkpoint->>'ts'` 的形态：langgraph 写的是 ISO 8601 带时区。**先按形态筛一遍再 cast**：
# 一条形态不对的行会让 `::timestamptz` 当场报错、把整次盘点打掉；筛过之后它落进
# "取不到时间"那一档 ⇒ 那条线程**永不裁**（判据缺失时不动手，见头注不变量②）。
_TS_OK = r"^\d{4}-\d{2}-\d{2}T"

PLAN_SQL = """
SELECT thread_id,
       count(*)                                   AS rows_n,
       max(ts)                                    AS last_at,
       count(*) FILTER (WHERE ts IS NULL)         AS undated
  FROM (
        SELECT thread_id,
               CASE WHEN checkpoint->>'ts' ~ %s THEN (checkpoint->>'ts')::timestamptz END AS ts
          FROM checkpoints
       ) t
 GROUP BY thread_id
 ORDER BY 1
"""


def cutoff_of(keep_days: float, now: datetime | None = None) -> datetime:
    """保留期的边界时刻（**带时区**，与 `timestamptz` 同族）。

    `now` 可注入是为了判据能钉住一个确定的时间点（不注入就取当前 UTC）。
    """
    now = datetime.now(timezone.utc) if now is None else now
    return now - timedelta(days=keep_days)


def plan_rows(rows: list[tuple], cutoff: datetime,
              max_threads: int = MAX_THREADS_DEFAULT) -> dict:
    """**纯函数**：盘出来的行 → 这一轮要裁谁。`--apply` 与 dry-run 共用这一份判据
    （两条路径各算一遍就是"看到的和做到的不一样"的来源）。

    行形状 = `(thread_id, rows_n, last_at, undated)`（`PLAN_SQL` 的四列）。

    返回 `{"prune": [...], "keep": [...], "undated": [...], "deferred": [...]}`：
      · `prune`    —— 最近活动早于 cutoff、这一轮要裁的线程（按最近活动**从老到新**）；
      · `deferred` —— 该裁但**超出 `max_threads` 预算**、留到下一轮的那些（同样是老到新）；
      · `undated`  —— **一行都取不到时间**的线程（永不裁，见头注不变量②）；
      · `keep`     —— 窗内的线程。

    ⚠️ "该裁"的判据是 `last_at is not None and last_at < cutoff`——**`None` 走 undated，
    绝不走 prune**。反过来写（把 None 当"很老"）会让一次形态漂移变成一次静默的全库删除。
    """
    prune, keep, undated = [], [], []
    for tid, rows_n, last_at, n_undated in rows:
        if last_at is None:
            undated.append({"thread_id": tid, "rows": rows_n, "undated_rows": n_undated})
        elif last_at < cutoff:
            prune.append({"thread_id": tid, "rows": rows_n, "last_at": last_at})
        else:
            keep.append({"thread_id": tid, "rows": rows_n, "last_at": last_at})
    prune.sort(key=lambda r: r["last_at"])
    deferred = prune[max_threads:] if max_threads >= 0 else []
    return {"prune": prune[:max_threads] if max_threads >= 0 else prune,
            "deferred": deferred, "keep": keep, "undated": undated}


# ── 连接 ─────────────────────────────────────────────────────────────────────
def connect(dsn: str):
    """开一条连接（`autocommit=True`：事务由 `conn.transaction()` 显式划，见 `apply_plan`）。"""
    import psycopg
    return psycopg.connect(dsn, autocommit=True)


def table_exists(conn, name: str) -> bool:
    """这个库的**当前 schema** 里有没有这张表（`to_regclass` 不抛，认不出就是 NULL）。"""
    row = conn.execute("SELECT to_regclass(%s) IS NOT NULL AS ok", (name,)).fetchone()
    if row is None:
        return False
    return bool(row[0] if not isinstance(row, dict) else row["ok"])


def scan(conn, cutoff: datetime, max_threads: int = MAX_THREADS_DEFAULT) -> dict:
    """只读：把库盘一遍，算出这一轮该裁谁。**不写一个字**。"""
    cur = conn.execute(PLAN_SQL, (_TS_OK,))
    rows = [tuple(r) if not isinstance(r, dict) else
            (r["thread_id"], r["rows_n"], r["last_at"], r["undated"]) for r in cur.fetchall()]
    pl = plan_rows(rows, cutoff, max_threads)
    pl["threads_total"] = len(rows)
    pl["cutoff"] = cutoff
    pl["ledger"] = _ledger_due(conn, cutoff)
    pl["fence_cutoff"] = min(cutoff, datetime.now(timezone.utc) - timedelta(days=FENCE_KEEP_DAYS))
    pl["deletion_fences"] = _fences_due(conn, pl["fence_cutoff"])
    return pl


def _fences_due(conn, cutoff):
    if not table_exists(conn, FENCE_TABLE):
        return {"exists": False, "due": 0}
    row = conn.execute(
        "SELECT count(*) FROM agent_deleted_threads WHERE deleted_at < %s", (cutoff,)
    ).fetchone()
    count = row[0] if not isinstance(row, dict) else next(iter(row.values()))
    return {"exists": True, "due": int(count or 0)}


def _ledger_due(conn, cutoff: datetime) -> dict:
    """账里早于 cutoff 的行有多少（表不在就如实说"没有这张表"）。"""
    if not table_exists(conn, LEDGER_TABLE):
        return {"exists": False, "due": 0}
    row = conn.execute(
        f"SELECT count(*) FROM {LEDGER_TABLE} WHERE {LEDGER_TS} < %s", (cutoff,)).fetchone()
    n = row[0] if not isinstance(row, dict) else row["ts"]
    return {"exists": True, "due": int(n or 0)}


# ── 执行 ─────────────────────────────────────────────────────────────────────
def apply_plan(conn, pl: dict) -> dict:
    """真删：**一个事务里**删 checkpoint 三张表 + 幂等账，**commit 前断言**。

    断言不成立 ⇒ 抛（`conn.transaction()` 随之回滚）⇒ 一个字都没删。
    这条是"账与 checkpoint 必须一起裁"的机器化：宁可什么都不删，也不留下半裁的状态
    （留下 checkpoint 却删了账 = 把 ADR-0012 要堵的洞重新打开）。
    """
    threads = [r["thread_id"] for r in pl["prune"]]
    cutoff = pl["cutoff"]
    out = {"threads": len(threads), "rows": {}, "ledger": 0, "errors": []}
    if not threads and not pl["ledger"]["due"] and not pl.get("deletion_fences", {}).get("due"):
        return out

    with conn.transaction():
        if threads:
            for table in THREAD_TABLES:
                out["rows"][table] = _exec_count(
                    conn, f"DELETE FROM {table} WHERE thread_id = ANY(%s)", (threads,))
        if pl["ledger"]["due"]:
            out["ledger"] = _exec_count(
                conn, f"DELETE FROM {LEDGER_TABLE} WHERE {LEDGER_TS} < %s", (cutoff,))
        _assert_clean(conn, threads, cutoff, pl["ledger"]["exists"])
        if pl.get("deletion_fences", {}).get("due"):
            out["deletion_fences"] = _exec_count(
                conn, "DELETE FROM agent_deleted_threads WHERE deleted_at < %s",
                (pl["fence_cutoff"],))
            if _scalar(conn, "SELECT count(*) FROM agent_deleted_threads WHERE deleted_at < %s",
                       (pl["fence_cutoff"],)):
                raise RuntimeError("过期会话删除屏障未清理，事务回滚")
    return out


def _exec_count(conn, sql: str, params: tuple) -> int:
    cur = conn.execute(sql, params)
    return int(cur.rowcount if cur.rowcount is not None and cur.rowcount >= 0 else 0)


def _assert_clean(conn, threads: list[str], cutoff: datetime, ledger: bool) -> None:
    """**commit 之前**跑的三条断言。任何一条不成立就抛 ⇒ 整个事务回滚。

    ① 被点名裁掉的那批线程，三张表里**一行都不剩**（漏删一张表 = 孤儿数据）；
    ② 那批线程在 `checkpoints` 里确实查不到了（① 的正面读数，防"rowcount 骗人"）；
    ③ 账里早于 cutoff 的行**一行都不剩**（表在场时才判）。
    """
    if threads:
        for table in THREAD_TABLES:
            n = _scalar(conn, f"SELECT count(*) FROM {table} WHERE thread_id = ANY(%s)",
                        (threads,))
            if n:
                raise RuntimeError(
                    f"裁完 {table} 还剩 {n} 行属于被点名的那批线程 —— 事务回滚，一个字都不删。"
                    "（三张表之间**没有外键**，所以每一张都得自己删干净。）")
    if ledger:
        n = _scalar(conn, f"SELECT count(*) FROM {LEDGER_TABLE} WHERE {LEDGER_TS} < %s",
                    (cutoff,))
        if n:
            raise RuntimeError(
                f"裁完幂等账还剩 {n} 行早于 cutoff —— 事务回滚。"
                "账与 checkpoint 必须一起裁（ADR-0012），半裁的状态比不裁更坏。")


def _scalar(conn, sql: str, params: tuple) -> int:
    row = conn.execute(sql, params).fetchone()
    if row is None:
        return 0
    return int(row[0] if not isinstance(row, dict) else list(row.values())[0])


# ── 报告 ─────────────────────────────────────────────────────────────────────
def _fmt_when(dt) -> str:
    return dt.astimezone().strftime("%Y-%m-%d %H:%M") if dt else "—"


def report(pl: dict, res: dict | None, dsn_shown: str) -> str:
    lines = [f"== checkpoint store 保留治理 [{dsn_shown}] ==",
             f"线程 {pl['threads_total']} 条；cutoff = {_fmt_when(pl['cutoff'])}"
             f"（保留期 {pl['keep_days']:g} 天）",
             f"{'已裁' if res else '将裁'} {len(pl['prune'])} 条线程"
             f"（共 {sum(r['rows'] for r in pl['prune'])} 行 checkpoint）"
             f"；窗内保留 {len(pl['keep'])} 条"]
    if pl["deferred"]:
        lines.append(f"⏳ 超预算（--max-threads {pl['max_threads']}）留到下一轮："
                     f"{len(pl['deferred'])} 条（都是比上面那批更老的）")
    if pl["undated"]:
        lines.append(f"⚠ {len(pl['undated'])} 条线程**取不到最近活动时间**"
                     f"（`checkpoint->>'ts'` 形态不认识）⇒ 本轮不裁："
                     f"{', '.join(str(r['thread_id']) for r in pl['undated'][:3])}")
    if pl["ledger"]["exists"]:
        lines.append(f"{'已裁' if res else '将裁'}幂等账 {pl['ledger']['due']} 行"
                     f"（`{LEDGER_TABLE}`，按 `{LEDGER_TS} < cutoff`）")
    else:
        lines.append(f"— `{LEDGER_TABLE}` 在这个库里**不存在**（图从没在挂 saver 的进程里"
                     "做过写操作）⇒ 账这一路本轮无事可做")
    if pl.get("deletion_fences", {}).get("exists"):
        lines.append(f"{'已裁' if res else '将裁'}过期删除屏障 {pl['deletion_fences']['due']} 行")
    if pl["prune"]:
        lines.append(f"{'已裁' if res else '将裁'}线程（从老到新，最多列 10 条）：")
        for r in pl["prune"][:10]:
            lines.append(f"    {r['thread_id']}  最近活动 {_fmt_when(r['last_at'])}"
                         f"  {r['rows']} 行")
        if len(pl["prune"]) > 10:
            lines.append(f"    …另 {len(pl['prune']) - 10} 条")
    if res:
        for table, n in res["rows"].items():
            lines.append(f"    删除 {table}：{n} 行")
        if res["ledger"]:
            lines.append(f"    删除 {LEDGER_TABLE}：{res['ledger']} 行")
    if not res:
        lines.append("（dry-run：什么都没动；确认后加 --apply）")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="checkpoint store 与幂等账的保留治理（默认只列不删）")
    ap.add_argument("--dsn", default=None, help="PG DSN（缺省读 CHECKPOINT_DSN）")
    ap.add_argument("--keep-days", type=float, default=KEEP_DAYS_DEFAULT,
                    help=f"线程最近活动早于这么多天就裁（默认 {KEEP_DAYS_DEFAULT}）")
    ap.add_argument("--max-threads", type=int, default=MAX_THREADS_DEFAULT,
                    help=f"一轮最多裁多少条线程（默认 {MAX_THREADS_DEFAULT}）")
    ap.add_argument("--apply", action="store_true", help="真删（不给就是 dry-run）")
    ap.add_argument("--json", action="store_true", help="只打印 JSON 摘要（管道/夜间日志用）")
    args = ap.parse_args(argv)

    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

    if args.dsn is not None:
        dsn = args.dsn.strip()
    else:
        # **走 settings 那一格，不自己读环境变量**：`CHECKPOINT_DSN` 正是 `checkpoint_dsn`
        # 的 env 名，而 settings 还会读 `.env`（生产就是这么配的）。自己 `os.environ.get`
        # 的话，夜间脚本跑起来会**看不见产线那一行**、静默报"这一层没量"——那是本仓最贵的
        # 那类失效（看着在跑、其实没跑）。
        from config.settings import settings
        dsn = (settings.checkpoint_dsn or "").strip()

    if not dsn:
        # **"没量"与"没有可删的"分开说**：这一行不写清楚，夜间汇总里那个 0 会被读成后者。
        print("⚠ 没有配 `CHECKPOINT_DSN`（也没有 --dsn）⇒ **这一层没量**，不是"
              "「跑过了、没有可删的」。图不挂 saver 时那个库根本不存在，本文件无从下手。")
        return 0

    from agent.checkpoint import redact  # 只回库名与主机，不回口令

    try:
        conn = connect(dsn)
    except Exception as e:                       # noqa: BLE001
        print(f"✗ 连不上 checkpoint 库（{redact(dsn)}）：{type(e).__name__}: {e}", file=sys.stderr)
        return 1

    try:
        if not table_exists(conn, "checkpoints"):
            print(f"⚠ {redact(dsn)} 里没有 `checkpoints` 表（图还没在这个库里建过表）"
                  "⇒ **这一层没量**，不是「跑过了、没有可删的」。")
            return 0
        pl = scan(conn, cutoff_of(args.keep_days), args.max_threads)
        pl["keep_days"] = args.keep_days
        pl["max_threads"] = args.max_threads
        res = apply_plan(conn, pl) if args.apply else None
    except Exception as e:                       # noqa: BLE001
        print(f"✗ 保留治理失败（事务已回滚）：{type(e).__name__}: {e}", file=sys.stderr)
        return 1
    finally:
        conn.close()

    if args.json:
        print(json.dumps({
            "dsn": redact(dsn), "applied": bool(res),
            "keep_days": args.keep_days, "max_threads": args.max_threads,
            "cutoff": pl["cutoff"].isoformat(),
            "threads_total": pl["threads_total"],
            "prune": [r["thread_id"] for r in pl["prune"]],
            "deferred": len(pl["deferred"]), "keep": len(pl["keep"]),
            "undated": len(pl["undated"]),
            "ledger_exists": pl["ledger"]["exists"], "ledger_due": pl["ledger"]["due"],
            "deleted_rows": (res or {}).get("rows", {}),
            "deleted_ledger": (res or {}).get("ledger", 0),
        }, ensure_ascii=False, indent=1))
    else:
        print(report(pl, res, redact(dsn)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
