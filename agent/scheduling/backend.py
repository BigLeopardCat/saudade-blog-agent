"""Public DBOS client adapter; no timer, queue or execution lease implementation."""

from contextlib import contextmanager
from hashlib import blake2b
from threading import BoundedSemaphore

import psycopg
from dbos import DBOSClient, DBOSPortableJSONSerializer, WorkflowSerializationFormat

APP = "saudade-automations"
SCHEMA = "saudade_automations"
CONTROL_QUEUE = "automation-timers"
EXECUTION_QUEUE = "automation-executions"
REMINDER_QUEUE = "automation-reminders"
ONCE = "saudade.automation.once.v1"
PERIODIC = "saudade.automation.periodic.v1"
EXECUTE = "saudade.automation.execute.v1"


def database_url(dsn):
    if dsn.startswith("postgres://"):
        dsn = "postgresql://" + dsn[len("postgres://") :]
    if dsn.startswith("postgresql://"):
        return dsn.replace("postgresql://", "postgresql+psycopg://", 1)
    if dsn.startswith("postgresql+psycopg://"):
        return dsn
    raise ValueError("scheduling requires a PostgreSQL DSN")


def storage_name(owner, identifier):
    return f"automation_{owner}_{identifier}"


class Backend:
    def __init__(self, dsn, *, schema=SCHEMA):
        self.dsn = database_url(dsn).replace("postgresql+psycopg://", "postgresql://", 1)
        self.client = DBOSClient(
            system_database_url=database_url(dsn),
            dbos_system_schema=schema,
            application_name=APP,
            serializer=DBOSPortableJSONSerializer(),
            system_database_pool_size=2,
            lazy=True,
            retry_connection_errors=False,
            observability_query_timeout_sec=5,
        )
        self._mutations = BoundedSemaphore(1)

    def close(self):
        self.client.destroy()

    @contextmanager
    def mutation(self, owner):
        # A separate, bounded connection avoids reserving the DBOS pool while
        # its public APIs open their own transactions. Never held during a run.
        key = int.from_bytes(
            blake2b(f"saudade.automation:{owner}".encode(), digest_size=8).digest(),
            "big",
            signed=True,
        )
        with self._mutations, psycopg.connect(self.dsn, connect_timeout=5) as conn:
            conn.execute("SET LOCAL lock_timeout = '5s'")
            conn.execute("SELECT pg_advisory_xact_lock(%s)", (key,))
            yield

    def once(self, owner, identifier):
        rows = self.client.list_workflows(
            workflow_ids=[storage_name(owner, identifier)], application_name=APP
        )
        return rows[0] if rows else None

    def recurring(self, owner, identifier):
        row = self.client.get_schedule(storage_name(owner, identifier))
        return row if row and row.get("application_name") == APP else None

    def enqueue_once(self, owner, identifier, payload):
        return self.client.enqueue(
            {
                "workflow_name": ONCE,
                "queue_name": CONTROL_QUEUE,
                "workflow_id": storage_name(owner, identifier),
                "authenticated_user": str(owner),
                "attributes": {
                    "definition": payload["definition"],
                    "schedule_id": identifier,
                    "start_at": payload["start_at"],
                    "paused": False,
                    "cancelled": False,
                },
                "serialization_type": WorkflowSerializationFormat.PORTABLE,
            },
            payload,
        )
