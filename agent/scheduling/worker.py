"""Run with python -m agent.scheduling.worker, independently of the HTTP workers."""

import signal
from contextlib import ExitStack
from datetime import datetime
from threading import Event

from dbos import DBOS, DBOSPortableJSONSerializer

from agent.scheduling import workflows
from agent.scheduling.backend import (
    APP,
    CONTROL_QUEUE,
    EXECUTION_QUEUE,
    REMINDER_QUEUE,
    SCHEMA,
    Backend,
    database_url,
    storage_name,
)
from agent.scheduling.factory import configured_dsn
from agent.scheduling.host import BlogHost
from agent.scheduling.models import Definition, utc_now
from agent.scheduling.readonly import ReadOnlyRunner
from config.settings import settings
from utils.logging import setup_logging


class WorkerActions:
    def __init__(self, backend, host, resources, stop_event=None):
        self.backend, self.host, self.resources = backend, host, resources
        self.runner = None
        self.stop_event = stop_event

    def enabled(self, payload, *, allow_paused=False):
        try:
            self.host.actor(payload["owner_id"])
        except PermissionError:
            if payload["definition"].get("cron"):
                self.backend.client.pause_schedule(
                    storage_name(payload["owner_id"], payload["schedule_id"])
                )
            return False
        definition = Definition.model_validate(payload["definition"])
        if definition.expires_at and utc_now() >= definition.expires_at:
            if definition.cron:
                self.backend.client.pause_schedule(
                    storage_name(payload["owner_id"], payload["schedule_id"])
                )
            return False
        if not definition.condition:
            due = datetime.fromisoformat(payload.get("occurrence_at") or payload["start_at"])
            if not definition.cron or payload.get("occurrence_at"):
                if (utc_now() - due).total_seconds() > definition.grace_seconds:
                    return False
        if definition.cron:
            row = self.backend.recurring(payload["owner_id"], payload["schedule_id"])
            original = {
                key: payload[key] for key in ("owner_id", "schedule_id", "definition", "start_at")
            }
            return bool(row and row["status"] == "ACTIVE" and row["context"] == original)
        row = self.backend.once(payload["owner_id"], payload["schedule_id"])
        return bool(
            row
            and (allow_paused or not (row.attributes or {}).get("paused"))
            and not (row.attributes or {}).get("cancelled")
        )

    def state(self, payload):
        if not self.enabled(payload, allow_paused=True):
            return "disabled"
        if not payload["definition"].get("cron"):
            row = self.backend.once(payload["owner_id"], payload["schedule_id"])
            if row and (row.attributes or {}).get("paused"):
                return "paused"
        return "ready"

    def deliverable(self, payload):
        if payload["definition"].get("cron"):
            return self.backend.recurring(payload["owner_id"], payload["schedule_id"]) is not None
        row = self.backend.once(payload["owner_id"], payload["schedule_id"])
        return row is not None and not (row.attributes or {}).get("cancelled")

    def perform(self, payload, run_id):
        definition = Definition.model_validate(payload["definition"])
        if not self.enabled(payload, allow_paused=definition.action != "publish_announcement"):
            return {"status": "blocked", "reason": "schedule_disabled"}
        if definition.action == "publish_announcement":
            return self.host.publish_announcement(
                payload["owner_id"], run_id, definition.title, definition.instruction
            )
        if definition.action == "reminder":
            return {"status": "completed", "text": definition.instruction}
        principal = self.host.actor(payload["owner_id"])
        if self.runner is None:
            self.runner = ReadOnlyRunner(self.resources, self.stop_event)
        return {"status": "completed", **self.runner.run(principal, run_id, definition.instruction)}


def main():
    setup_logging()
    if not settings.scheduling_enabled:
        raise RuntimeError("set SCHEDULING_ENABLED=true to start the schedule worker")
    dsn = configured_dsn()
    DBOS(
        config={
            "name": APP,
            "system_database_url": database_url(dsn),
            "dbos_system_schema": SCHEMA,
            "serializer": DBOSPortableJSONSerializer(),
            "enable_otlp": False,
            "max_executor_threads": 4,
            "sys_db_pool_size": 4,
            "application_version": "saudade-scheduling-v1",
        }
    )
    stop = Event()
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, lambda *_: stop.set())
    with ExitStack() as resources:
        backend = Backend(dsn)
        host = BlogHost()
        resources.callback(backend.close)
        resources.callback(host.close)
        workflows.configure(WorkerActions(backend, host, resources))
        try:
            DBOS.launch()
            DBOS.register_queue(CONTROL_QUEUE, worker_concurrency=100)
            DBOS.register_queue(EXECUTION_QUEUE, worker_concurrency=1, global_concurrency=1)
            DBOS.register_queue(REMINDER_QUEUE, worker_concurrency=2, global_concurrency=2)
            stop.wait()
        finally:
            # Stop is cooperative; allow the bounded model request to leave its checkpoint
            # before closing resources. Pending durable timers resume on the next worker.
            DBOS.destroy(workflow_completion_timeout_sec=95)
            workflows.configure(None)


if __name__ == "__main__":
    main()
