"""Bounded PostgreSQL retention with conversation locks and resumable-task protection.

Dry-run lists age-based candidates. Applying rechecks each candidate under the
same advisory lock as execution and deletes through LangGraph's public saver API.
"""

import argparse
import json
import logging
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

KEEP_DAYS_DEFAULT = 30
FENCE_KEEP_DAYS = 30
MAX_THREADS_DEFAULT = 200
THREAD_TABLES = ("checkpoints", "checkpoint_blobs", "checkpoint_writes")
LEDGER_TABLE = "agent_write_idem"
FENCE_TABLE = "agent_deleted_threads"
TURN_FENCE_TABLE = "agent_deleted_turns"
_TS_OK = r"^\d{4}-\d{2}-\d{2}T"
logger = logging.getLogger(__name__)
PLAN_SQL = """
SELECT thread_id, count(*) AS rows_n, max(ts) AS last_at,
       count(*) FILTER (WHERE ts IS NULL) AS undated
FROM (
    SELECT thread_id,
           CASE WHEN checkpoint->>'ts' ~ %s THEN (checkpoint->>'ts')::timestamptz END AS ts
    FROM checkpoints WHERE (%s::text IS NULL OR thread_id = %s)
) t GROUP BY thread_id ORDER BY thread_id
"""


def cutoff_of(keep_days: float, now: datetime | None = None) -> datetime:
    return (now or datetime.now(timezone.utc)) - timedelta(days=keep_days)


def plan_rows(rows: list[tuple], cutoff: datetime,
              max_threads: int = MAX_THREADS_DEFAULT) -> dict:
    prune, keep, undated = [], [], []
    for tid, rows_n, last_at, n_undated in rows:
        if last_at is None or n_undated:
            undated.append({"thread_id": tid, "rows": rows_n, "undated_rows": n_undated})
        elif last_at < cutoff:
            prune.append({"thread_id": tid, "rows": rows_n, "last_at": last_at})
        else:
            keep.append({"thread_id": tid, "rows": rows_n, "last_at": last_at})
    prune.sort(key=lambda row: (row["last_at"], row["thread_id"]))
    limit = max(0, max_threads)
    return {"prune": prune[:limit], "deferred": prune[limit:],
            "keep": keep, "undated": undated}


def connect(dsn: str):
    import psycopg
    return psycopg.connect(dsn, autocommit=True)


def _scalar(conn, query, params=()):
    row = conn.execute(query, params).fetchone()
    if row is None:
        return 0
    return next(iter(row.values())) if isinstance(row, dict) else row[0]


def table_exists(conn, name: str) -> bool:
    return bool(_scalar(conn, "SELECT to_regclass(%s) IS NOT NULL", (name,)))


def _activity(conn, thread_id=None):
    rows = conn.execute(PLAN_SQL, (_TS_OK, thread_id, thread_id)).fetchall()
    return [tuple(row[key] for key in ("thread_id", "rows_n", "last_at", "undated"))
            if isinstance(row, dict) else tuple(row) for row in rows]


def scan(conn, cutoff: datetime, max_threads: int = MAX_THREADS_DEFAULT) -> dict:
    rows = _activity(conn)
    plan = plan_rows(rows, cutoff, max_threads)
    plan.update(threads_total=len(rows), cutoff=cutoff)
    plan["ledger"] = {"exists": table_exists(conn, LEDGER_TABLE), "due": 0}
    if plan["ledger"]["exists"]:
        # Expired signed approvals cannot authorize a fresh write. Unknown intents
        # remain as evidence even after completed receipts reach the retention limit.
        plan["ledger"]["due"] = int(_scalar(conn, "SELECT count(*) FROM agent_write_idem "
            "WHERE status='done' AND ts < %s", (cutoff,)))
    plan["fence_cutoff"] = min(cutoff, cutoff_of(FENCE_KEEP_DAYS))
    for field, table in (("deletion_fences", FENCE_TABLE), ("turn_fences", TURN_FENCE_TABLE)):
        plan[field] = {"exists": table_exists(conn, table), "due": 0}
        if plan[field]["exists"]:
            plan[field]["due"] = int(_scalar(conn,
                f"SELECT count(*) FROM {table} WHERE deleted_at < %s", (plan["fence_cutoff"],)))
    return plan


def _recoverable(store, thread_id):
    snapshot = store.get_state({"configurable": {"thread_id": thread_id}})
    values = snapshot.values or {}
    if "run" not in values:
        return "unknown_state"
    if snapshot.next or snapshot.tasks or any(values.get(key) for key in (
            "run", "turn", "approval", "pending_confirm", "pending_action", "approved_result")):
        return "recoverable"
    return None


def _delete_task_state(conn, thread_id):
    cid = thread_id.removeprefix("conv_")
    if not thread_id.startswith("conv_") or not cid.isdecimal():
        return {}
    queries = {
        "agent_task_inputs": ("state #>> '{scope,conversation_id}' = %s", cid),
        "agent_task_usage": ("conversation_id = %s", int(cid)),
    }
    removed = {}
    for table, (where, value) in queries.items():
        if table_exists(conn, table):
            removed[table] = conn.execute(
                f"DELETE FROM {table} WHERE {where}", (value,)).rowcount
    return removed


def _assert_clean(conn, thread_id):
    for table in THREAD_TABLES:
        if _scalar(conn, f"SELECT count(*) FROM {table} WHERE thread_id=%s", (thread_id,)):
            raise RuntimeError(f"Checkpoint deletion incomplete: {table}")


def _retire_thread(conn, store, thread_id, cutoff):
    from agent.runtime.turns import conversation_lock_key

    with conn.transaction():
        locked = _scalar(conn, "SELECT pg_try_advisory_xact_lock(%s)",
                         (conversation_lock_key(thread_id),))
        if not locked:
            return {"skipped": "busy"}
        fresh = plan_rows(_activity(conn, thread_id), cutoff, 1)
        if not fresh["prune"]:
            return {"skipped": "activity_changed"}
        reason = _recoverable(store, thread_id)
        if reason:
            return {"skipped": reason}
        rows = {table: int(_scalar(conn, f"SELECT count(*) FROM {table} WHERE thread_id=%s",
                                  (thread_id,))) for table in THREAD_TABLES}
        store.saver.delete_thread(thread_id)
        rows.update(_delete_task_state(conn, thread_id))
        _assert_clean(conn, thread_id)
        return {"rows": rows}


def apply_plan(conn, plan: dict) -> dict:
    from langgraph.checkpoint.postgres import PostgresSaver

    from agent.runtime.checkpoint_conversations import CheckpointConversationStore

    store = CheckpointConversationStore(PostgresSaver(conn))
    result = {"threads": 0, "deleted_threads": [], "rows": {}, "ledger": 0,
              "deletion_fences": 0, "skipped": []}
    for candidate in plan["prune"]:
        thread_id = candidate["thread_id"]
        retired = _retire_thread(conn, store, thread_id, plan["cutoff"])
        if retired.get("skipped"):
            result["skipped"].append({"thread_id": thread_id, "reason": retired["skipped"]})
            continue
        result["threads"] += 1
        result["deleted_threads"].append(thread_id)
        logger.info("checkpoint_retired thread_id=%s rows=%s", thread_id, retired["rows"])
        for table, count in retired["rows"].items():
            result["rows"][table] = result["rows"].get(table, 0) + count
    with conn.transaction():
        if plan["ledger"]["exists"]:
            result["ledger"] = conn.execute("DELETE FROM agent_write_idem "
                "WHERE status='done' AND ts < %s", (plan["cutoff"],)).rowcount
        for field, table in (("deletion_fences", FENCE_TABLE), ("turn_fences", TURN_FENCE_TABLE)):
            if plan.get(field, {}).get("exists"):
                result[field] = conn.execute(f"DELETE FROM {table} WHERE deleted_at < %s",
                                             (plan["fence_cutoff"],)).rowcount
    return result


def report(plan, result, dsn_shown):
    summary = {"database": dsn_shown, "cutoff": plan["cutoff"].isoformat(),
               "candidates": [row["thread_id"] for row in plan["prune"]],
               "recent": len(plan["keep"]), "undated": len(plan["undated"]),
               "deferred": len(plan["deferred"]), "applied": result is not None,
               "result": result}
    return json.dumps(summary, ensure_ascii=False)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dsn", default=None)
    parser.add_argument("--keep-days", type=float, default=KEEP_DAYS_DEFAULT)
    parser.add_argument("--max-threads", type=int, default=MAX_THREADS_DEFAULT)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    if not KEEP_DAYS_DEFAULT <= args.keep_days < float("inf") or args.max_threads < 0:
        parser.error("Retention must be at least 30 days and max-threads must be nonnegative")
    from agent.checkpoint import redact
    from config.settings import settings

    dsn = (args.dsn if args.dsn is not None else settings.checkpoint_dsn or "").strip()
    if not dsn:
        print(json.dumps({"evaluated": False, "reason": "CHECKPOINT_DSN not configured"}))
        return 0
    try:
        with connect(dsn) as conn:
            if not table_exists(conn, "checkpoints"):
                print(json.dumps({"evaluated": False, "reason": "checkpoints table absent"}))
                return 0
            plan = scan(conn, cutoff_of(args.keep_days), args.max_threads)
            result = apply_plan(conn, plan) if args.apply else None
    except Exception as exc:  # noqa: BLE001
        print(f"Checkpoint retention failed: {type(exc).__name__}", file=sys.stderr)
        return 1
    print(report(plan, result, redact(dsn)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
