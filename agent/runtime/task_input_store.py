"""Short, atomic inbox transactions; checkpoint execution keeps its own lock."""

import copy
import threading
import time
from contextlib import contextmanager

TERMINAL_RETENTION_SECONDS = 24 * 3600


def setup(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS agent_task_inputs (
        task_id TEXT PRIMARY KEY,
        state JSONB NOT NULL,
        expires_at TIMESTAMPTZ
    )""")
    conn.execute("ALTER TABLE agent_task_inputs ALTER COLUMN expires_at DROP NOT NULL")
    conn.execute("UPDATE agent_task_inputs SET expires_at=NULL, "
                 "state=jsonb_set(state,'{expires}','null'::jsonb) "
                 "WHERE state->>'phase' NOT IN ('completed','superseded') "
                 "AND expires_at IS NOT NULL")
    conn.execute("CREATE INDEX IF NOT EXISTS agent_task_inputs_expiry "
                 "ON agent_task_inputs(expires_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS agent_task_inputs_owner_phase "
                 "ON agent_task_inputs ((state #>> '{scope,uid}'), (state->>'phase'))")


class PostgresInputs:
    def __init__(self, pool):
        self.pool = pool

    def delete_task(self, task_id):
        with self.pool.connection() as conn:
            conn.execute("DELETE FROM agent_task_inputs WHERE task_id=%s", (task_id,))

    def delete_conversation(self, conversation_id):
        with self.pool.connection() as conn:
            conn.execute("DELETE FROM agent_task_inputs "
                         "WHERE state #>> '{scope,conversation_id}' = %s", (str(conversation_id),))

    def paused(self, uid, role, limit):
        with self.pool.connection() as conn:
            rows = conn.execute("SELECT task_id,state FROM agent_task_inputs "
                "WHERE state #>> '{scope,uid}'=%s AND state #>> '{scope,role}'=%s "
                "AND state->>'phase'='paused' AND state #> '{scope,conversation_id}' IS NOT NULL "
                "ORDER BY state->'created_at' DESC NULLS LAST,task_id LIMIT %s",
                (str(uid), role, limit)).fetchall()
            return [(row["task_id"], row["state"]) for row in rows]

    @contextmanager
    def edit(self, task_id, initial=None):
        from psycopg.types.json import Jsonb

        with self.pool.connection() as conn, conn.transaction():
            if initial is not None:
                if initial["scope"].get("conversation_id"):
                    expiry = time.time() + TERMINAL_RETENTION_SECONDS
                    conn.execute("UPDATE agent_task_inputs SET state=state || %s, "
                        "expires_at=to_timestamp(%s) "
                        "WHERE state->'scope'=%s AND state->>'phase'='paused' AND task_id<>%s",
                        (Jsonb({"phase": "superseded", "expires": expiry}), expiry,
                         Jsonb(initial["scope"]), task_id))
                conn.execute("DELETE FROM agent_task_inputs WHERE expires_at < now() "
                             "AND state->>'phase' IN ('completed','superseded')")
                conn.execute("INSERT INTO agent_task_inputs(task_id,state,expires_at) "
                             "VALUES (%s,%s,to_timestamp(%s)) "
                             "ON CONFLICT DO NOTHING",
                             (task_id, Jsonb(initial), initial["expires"]))
            row = conn.execute("SELECT state FROM agent_task_inputs WHERE task_id=%s "
                               "AND (expires_at IS NULL OR expires_at > now()) FOR UPDATE",
                               (task_id,)).fetchone()
            state = row["state"] if row else None
            yield state
            if state is not None:
                conn.execute("UPDATE agent_task_inputs SET state=%s,expires_at=to_timestamp(%s) "
                             "WHERE task_id=%s", (Jsonb(state), state["expires"], task_id))


class MemoryInputs:
    """Development adapter with the same transaction and rollback semantics."""

    def __init__(self):
        self.rows, self.lock = {}, threading.Lock()

    def delete_task(self, task_id):
        with self.lock:
            self.rows.pop(task_id, None)

    def delete_conversation(self, conversation_id):
        with self.lock:
            self.rows = {key: value for key, value in self.rows.items()
                         if str(value["scope"].get("conversation_id")) != str(conversation_id)}

    def paused(self, uid, role, limit):
        with self.lock:
            rows = [(key, copy.deepcopy(state)) for key, state in self.rows.items()
                    if state["scope"].get("uid") == uid and state["scope"].get("role") == role
                    and state["scope"].get("conversation_id")
                    and state["phase"] == "paused"]
            return sorted(rows, key=lambda row: (-row[1].get("created_at", 0), row[0]))[:limit]

    @contextmanager
    def edit(self, task_id, initial=None):
        with self.lock:
            self.rows = {key: value for key, value in self.rows.items()
                         if value.get("expires") is None or value["expires"] > time.time()}
            staged = copy.deepcopy(self.rows)
            if initial is not None and initial["scope"].get("conversation_id"):
                for key, row in staged.items():
                    if (key != task_id and row["scope"] == initial["scope"]
                            and row["phase"] == "paused"):
                        row["phase"] = "superseded"
                        row["expires"] = time.time() + TERMINAL_RETENTION_SECONDS
            state = copy.deepcopy(staged.get(task_id, initial))
            yield state
            if state is not None:
                staged[task_id] = state
                self.rows = staged
