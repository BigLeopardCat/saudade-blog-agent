"""Short, atomic inbox transactions; checkpoint execution keeps its own lock."""

import copy
import threading
import time
from contextlib import contextmanager


def setup(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS agent_task_inputs (
        task_id TEXT PRIMARY KEY,
        state JSONB NOT NULL,
        expires_at TIMESTAMPTZ NOT NULL
    )""")
    conn.execute("CREATE INDEX IF NOT EXISTS agent_task_inputs_expiry "
                 "ON agent_task_inputs(expires_at)")


class PostgresInputs:
    def __init__(self, pool):
        self.pool = pool

    def delete_conversation(self, conversation_id):
        with self.pool.connection() as conn:
            conn.execute("DELETE FROM agent_task_inputs "
                         "WHERE state #>> '{scope,conversation_id}' = %s", (str(conversation_id),))

    @contextmanager
    def edit(self, task_id, initial=None):
        from psycopg.types.json import Jsonb

        with self.pool.connection() as conn, conn.transaction():
            if initial is not None:
                conn.execute("DELETE FROM agent_task_inputs WHERE expires_at < now()")
                conn.execute("INSERT INTO agent_task_inputs VALUES (%s,%s,to_timestamp(%s)) "
                             "ON CONFLICT DO NOTHING",
                             (task_id, Jsonb(initial), initial["expires"]))
            row = conn.execute("SELECT state FROM agent_task_inputs WHERE task_id=%s "
                               "AND expires_at > now() FOR UPDATE", (task_id,)).fetchone()
            state = row["state"] if row else None
            yield state
            if state is not None:
                conn.execute("UPDATE agent_task_inputs SET state=%s WHERE task_id=%s",
                             (Jsonb(state), task_id))


class MemoryInputs:
    """Development adapter with the same transaction and rollback semantics."""

    def __init__(self):
        self.rows, self.lock = {}, threading.Lock()

    def delete_conversation(self, conversation_id):
        with self.lock:
            self.rows = {key: value for key, value in self.rows.items()
                         if str(value["scope"]["conversation_id"]) != str(conversation_id)}

    @contextmanager
    def edit(self, task_id, initial=None):
        with self.lock:
            self.rows = {key: value for key, value in self.rows.items()
                         if value["expires"] > time.time()}
            state = copy.deepcopy(self.rows.get(task_id, initial))
            yield state
            if state is not None:
                self.rows[task_id] = state
