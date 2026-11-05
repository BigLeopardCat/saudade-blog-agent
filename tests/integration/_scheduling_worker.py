"""Isolated DBOS worker used only by the explicit PostgreSQL integration suite."""

import json
import sys
import time
from contextlib import ExitStack
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import psycopg
from dbos import DBOS, DBOSPortableJSONSerializer
from langchain_core.messages import AIMessage, AIMessageChunk

from agent.principal import Principal
from agent.scheduling import readonly, workflows
from agent.scheduling.backend import (
    APP,
    CONTROL_QUEUE,
    EXECUTION_QUEUE,
    REMINDER_QUEUE,
    Backend,
    database_url,
)
from agent.scheduling.worker import WorkerActions
from config.settings import settings

DSN, SCHEMA, READY = sys.argv[1:]
url = urlsplit(DSN)
params = dict(parse_qsl(url.query))
params["options"] = "-csearch_path=" + SCHEMA
settings.checkpoint_dsn = urlunsplit(
    (url.scheme, url.netloc, url.path, urlencode(params), url.fragment)
)
settings.checkpoint_forbid_db = ""


class Model:
    max_tokens = 64

    def bind_tools(self, tools, **kwargs):
        return self

    def stream(self, messages, **kwargs):
        yield AIMessageChunk(content="isolated background answer")

    def invoke(self, messages, **kwargs):
        return AIMessage(content="isolated background answer")


readonly.get_llm = lambda **_: Model()


def connection():
    return psycopg.connect(DSN, options=f"-c search_path={SCHEMA}")


class Host:
    def actor(self, owner):
        with connection() as conn:
            role = conn.execute("SELECT role FROM test_actors WHERE id=%s", (owner,)).fetchone()
        if not role or role[0] not in {"admin", "superadmin"}:
            raise PermissionError("test actor is no longer authorized")
        return Principal(owner, role[0], "assertion")

    def article_public(self, owner, identifier):
        self.actor(owner)
        with connection() as conn:
            return conn.execute(
                "SELECT value FROM test_flags WHERE key='article_public'"
            ).fetchone()[0]

    def publish_announcement(self, owner, run_id, title, text):
        self.actor(owner)
        with connection() as conn:
            conn.execute(
                "INSERT INTO test_publications VALUES (%s,%s,%s,%s) "
                "ON CONFLICT(run_id) DO NOTHING", (run_id, owner, title, text),
            )
        while True:
            with connection() as conn:
                block = conn.execute(
                    "SELECT value FROM test_flags WHERE key='block_publish_after_commit'"
                ).fetchone()[0]
            if not block:
                break
            time.sleep(0.05)
        return {"status": "completed", "text": "已发布审批原文"}

    def change(self, owner, run_id, write, title):
        return self.publish_announcement(owner, run_id, title,
                                         json.dumps(write, ensure_ascii=False, sort_keys=True))

    def notify(self, owner, run_id, title, text):
        self.actor(owner)
        with connection() as conn:
            conn.execute("INSERT INTO test_attempts(run_id) VALUES (%s)", (run_id,))
        while True:
            with connection() as conn:
                block = conn.execute(
                    "SELECT value FROM test_flags WHERE key='block_notify'"
                ).fetchone()[0]
            if not block:
                break
            time.sleep(0.05)
        with connection() as conn:
            conn.execute(
                "INSERT INTO test_notifications(run_id,owner_id,body) VALUES (%s,%s,%s) "
                "ON CONFLICT(run_id) DO NOTHING",
                (run_id, owner, text),
            )
        return {"notification_id": 1}


class Actions(WorkerActions):
    def perform(self, payload, run_id):
        with connection() as conn:
            conn.execute("INSERT INTO test_effects(run_id) VALUES (%s)", (run_id,))
        return super().perform(payload, run_id)


DBOS(
    config={
        "name": APP,
        "system_database_url": database_url(DSN),
        "dbos_system_schema": SCHEMA,
        "serializer": DBOSPortableJSONSerializer(),
        "enable_otlp": False,
        "max_executor_threads": 4,
        "sys_db_pool_size": 4,
        "application_version": "saudade-scheduling-v1",
    }
)
with ExitStack() as resources:
    backend = Backend(DSN, schema=SCHEMA)
    resources.callback(backend.close)
    workflows.configure(Actions(backend, Host(), resources))
    DBOS.launch()
    DBOS.register_queue(CONTROL_QUEUE, worker_concurrency=100)
    DBOS.register_queue(EXECUTION_QUEUE, worker_concurrency=1, global_concurrency=1)
    DBOS.register_queue(REMINDER_QUEUE, worker_concurrency=2, global_concurrency=2)
    Path(READY).touch()
    while True:
        time.sleep(1)
