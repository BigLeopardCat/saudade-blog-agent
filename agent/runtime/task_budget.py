"""Durable task usage; provider reservations survive cancelled graph nodes."""

import threading


def setup(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS agent_task_usage (
        task_id TEXT PRIMARY KEY,
        conversation_id BIGINT NOT NULL,
        scope JSONB NOT NULL,
        spent JSONB NOT NULL
    )""")
    conn.execute("CREATE INDEX IF NOT EXISTS agent_task_usage_conversation "
                 "ON agent_task_usage(conversation_id)")


class PostgresUsage:
    def __init__(self, pool):
        self.pool = pool

    def load(self, task, *, create=True):
        from psycopg.types.json import Jsonb

        with self.pool.connection() as conn, conn.transaction():
            if create:
                conn.execute("INSERT INTO agent_task_usage VALUES (%s,%s,%s,%s) "
                             "ON CONFLICT DO NOTHING",
                             (task["id"], task["scope"]["conversation_id"],
                              Jsonb(task["scope"]), Jsonb({})))
            row = conn.execute("SELECT scope, spent FROM agent_task_usage WHERE task_id=%s",
                               (task["id"],)).fetchone()
            if row is None:
                raise ValueError("task_usage_unavailable")
            if row["scope"] != task["scope"]:
                raise ValueError("task_usage_scope_mismatch")
            return row["spent"]

    def save(self, task, spent):
        from psycopg.types.json import Jsonb

        with self.pool.connection() as conn:
            changed = conn.execute("UPDATE agent_task_usage SET spent=%s "
                "WHERE task_id=%s AND scope=%s", (Jsonb(spent), task["id"], Jsonb(task["scope"])))
            if changed.rowcount != 1:
                raise ValueError("task_usage_scope_mismatch")

    def delete_conversation(self, conversation_id):
        with self.pool.connection() as conn:
            conn.execute("DELETE FROM agent_task_usage WHERE conversation_id=%s",
                         (conversation_id,))


class MemoryUsage:
    def __init__(self):
        self.rows, self.lock = {}, threading.Lock()

    def load(self, task, *, create=True):
        with self.lock:
            if create:
                self.rows.setdefault(task["id"], (dict(task["scope"]), {}))
            if task["id"] not in self.rows:
                raise ValueError("task_usage_unavailable")
            scope, spent = self.rows[task["id"]]
            if scope != task["scope"]:
                raise ValueError("task_usage_scope_mismatch")
            return dict(spent)

    def save(self, task, spent):
        with self.lock:
            scope, _ = self.rows[task["id"]]
            if scope != task["scope"]:
                raise ValueError("task_usage_scope_mismatch")
            self.rows[task["id"]] = (scope, dict(spent))

    def delete_conversation(self, conversation_id):
        with self.lock:
            self.rows = {key: row for key, row in self.rows.items()
                         if row[0]["conversation_id"] != int(conversation_id)}


def for_saver(saver):
    from agent.runtime.checkpoint_store import postgres

    if postgres(saver):
        return PostgresUsage(saver.conn)
    if not hasattr(saver, "_task_usage"):
        saver._task_usage = MemoryUsage()
    return saver._task_usage
