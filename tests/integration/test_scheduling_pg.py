"""Real timer/queue recovery. Runs only against an explicitly supplied disposable DSN."""

import os
import subprocess
import sys
import time
import uuid
from contextlib import ExitStack
from datetime import timedelta
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import psycopg
import pytest
from psycopg import sql

from agent.principal import Principal
from agent.scheduling.backend import Backend
from agent.scheduling.checkpoints import Cleanup
from agent.scheduling.models import utc_now
from agent.scheduling.service import ScheduleConflict, ScheduleNotFound, ScheduleService
from config.settings import settings

ADMIN = Principal(7, "admin", "assertion")


def eventually(function, timeout=30):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = function()
        if value:
            return value
        time.sleep(0.05)
    pytest.fail("durable scheduling condition was not reached before deadline")


@pytest.fixture
def site(tmp_path):
    dsn = os.getenv("SAUDADE_SCHEDULING_TEST_DSN")
    if not dsn:
        pytest.skip("requires explicit disposable SAUDADE_SCHEDULING_TEST_DSN")
    schema = "scheduling_" + uuid.uuid4().hex
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))

    def query(statement, args=()):
        with psycopg.connect(dsn, options=f"-c search_path={schema}") as conn:
            result = conn.execute(statement, args)
            return result.fetchall() if result.description else []

    query(
        "CREATE TABLE test_actors(id integer primary key,role text);"
        "INSERT INTO test_actors VALUES (7,'admin'),(8,'admin');"
        "CREATE TABLE test_flags(key text primary key,value boolean);"
        "INSERT INTO test_flags VALUES ('block_notify',false),('article_public',false);"
        "CREATE TABLE test_effects(id serial,run_id text);"
        "CREATE TABLE test_attempts(id serial,run_id text);"
        "CREATE TABLE test_notifications(run_id text primary key, owner_id integer,body text);"
        "CREATE TABLE test_publications(run_id text primary key,"
        "owner_id integer,title text,body text);"
        "INSERT INTO test_flags VALUES ('block_publish_after_commit',false)"
    )
    processes, logs = [], []

    def start():
        ready = tmp_path / (uuid.uuid4().hex + ".ready")
        log = (tmp_path / (uuid.uuid4().hex + ".log")).open("w")
        logs.append(log)
        process = subprocess.Popen(
            [sys.executable, "tests/integration/_scheduling_worker.py", dsn, schema, str(ready)],
            cwd=Path(__file__).resolve().parents[2],
            stdout=log,
            stderr=subprocess.STDOUT,
            env={**os.environ, "PYTHONPATH": ".", "SAUDADE_IGNORE_ENV_FILE": "1"},
        )
        processes.append(process)
        eventually(lambda: ready.exists() or process.poll() is not None)
        assert process.poll() is None, log.name
        return process

    backend = None
    try:
        start()
        backend = Backend(dsn, schema=schema)

        def actor(owner):
            role = query("SELECT role FROM test_actors WHERE id=%s", (owner,))[0][0]
            return Principal(owner, role, "assertion")

        yield ScheduleService(backend, actor), query, start, processes
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=15)
        if backend:
            backend.close()
        for log in logs:
            log.close()
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


def create(service, **values):
    ident = "sc_" + uuid.uuid4().hex
    definition = {
        "title": "提醒",
        "instruction": "隔离测试",
        "action": "reminder",
        "delay_seconds": 5,
        **values,
    }
    return ident, definition, service.create(ADMIN, ident, definition)


def test_timing_replay_owner_isolation_pause_and_resume(site):
    service, query, _, _ = site
    ident, definition, first = create(service)
    assert first["changed"]
    assert service.backend.once(ADMIN.uid, ident).status == "DELAYED"
    assert not service.create(ADMIN, ident, definition)["changed"]
    with pytest.raises(ScheduleConflict):
        service.create(ADMIN, ident, {**definition, "instruction": "不同内容"})
    with pytest.raises(ScheduleNotFound):
        service.get(Principal(8, "admin", "assertion"), ident)
    paused = service.control(ADMIN, ident, "pause", first["schedule"]["version"])["schedule"]
    assert paused["status"] == "paused"
    time.sleep(6)
    assert service.get(ADMIN, ident)["status"] == "paused"
    assert query("SELECT count(*) FROM test_notifications")[0][0] == 0
    with pytest.raises(ScheduleConflict):
        service.control(ADMIN, ident, "resume", first["schedule"]["version"])
    service.control(ADMIN, ident, "resume", paused["version"])
    eventually(lambda: service.get(ADMIN, ident)["status"] == "finished")
    assert query("SELECT count(*) FROM test_notifications")[0][0] == 1
    runs = service.runs(ADMIN, ident)
    assert runs["items"][0]["result"]["status"] == "completed"
    assert service.result(ADMIN, ident, runs["items"][0]["id"])["result"]["text"] == "隔离测试"
    assert service.runs(ADMIN, ident, offset=1)["items"] == []
    from agent.scheduling.retention import collect

    live_id, _, _ = create(service, delay_seconds=86400)
    preview = collect(service.backend, now=lambda: utc_now() + timedelta(days=31))
    assert preview["candidates"] == [runs["items"][0]["id"]]
    assert preview["deleted"] == []
    with ExitStack() as resources:
        deleted = collect(
            service.backend,
            apply=True,
            now=lambda: utc_now() + timedelta(days=31),
            cleanup=Cleanup(service.backend.client, resources),
        )
    assert deleted["deleted"] == preview["candidates"]
    assert service.backend.once(7, ident) is None
    assert service.get(ADMIN, live_id)["status"] == "scheduled"
    with pytest.raises(ScheduleNotFound):
        service.result(Principal(8, "admin", "assertion"), ident, runs["items"][0]["id"])


def test_kill_recovery_retains_completed_steps_and_deduplicates_delivery(site):
    service, query, start, processes = site
    query("UPDATE test_flags SET value=true WHERE key='block_notify'")
    ident, _, _ = create(service)
    eventually(lambda: query("SELECT count(*) FROM test_attempts")[0][0] == 1)
    processes[0].kill()
    processes[0].wait(timeout=10)
    query("UPDATE test_flags SET value=false WHERE key='block_notify'")
    start()
    eventually(lambda: service.get(ADMIN, ident)["status"] == "finished")
    assert query("SELECT count(*) FROM test_effects")[0][0] == 1
    assert query("SELECT count(*) FROM test_attempts")[0][0] == 2
    assert query("SELECT count(*) FROM test_notifications")[0][0] == 1


def test_role_change_cancel_and_condition_wait_never_run_effect(site):
    service, query, _, _ = site
    ident, _, created = create(service)
    service.control(ADMIN, ident, "cancel", created["schedule"]["version"])
    assert service.get(ADMIN, ident)["status"] == "cancelled"
    waiting, _, _ = create(
        service,
        condition={"kind": "article_public", "article_id": 54},
        expires_at=(utc_now() + timedelta(minutes=2)).isoformat(),
    )
    query("UPDATE test_actors SET role='user' WHERE id=7")
    with pytest.raises(PermissionError):
        service.list(ADMIN)
    eventually(lambda: service.backend.once(7, waiting).status == "SUCCESS")
    assert service.backend.once(7, waiting).output["status"] == "blocked"
    assert query("SELECT count(*) FROM test_notifications")[0][0] == 0
    assert query("SELECT count(*) FROM test_effects")[0][0] == 0


def test_cron_uses_native_persistent_controls(site):
    service, _, _, _ = site
    ident, _, created = create(service, delay_seconds=None, cron="0 8 * * *")
    assert service.get(ADMIN, ident)["status"] == "active"
    paused = service.control(ADMIN, ident, "pause", created["schedule"]["version"])["schedule"]
    assert service.backend.recurring(7, ident)["status"] == "PAUSED"
    resumed = service.control(ADMIN, ident, "resume", paused["version"])["schedule"]
    assert resumed["status"] == "active"
    service.control(ADMIN, ident, "cancel", resumed["version"])
    assert service.backend.recurring(7, ident) is None


def test_real_scheduled_agent_uses_isolated_checkpoint_and_cleanup_follows_children(
    site, monkeypatch
):
    service, query, _, _ = site
    ident, _, _ = create(service, action="read_only_agent")
    eventually(lambda: service.get(ADMIN, ident)["status"] == "finished")
    root = service.runs(ADMIN, ident)["items"][0]["id"]
    result = service.result(ADMIN, ident, root)["result"]
    assert result["status"] == "completed" and result["text"] == "isolated background answer"
    thread = result["thread_id"]
    assert thread.startswith("background_")
    assert query("SELECT 1 FROM checkpoints WHERE thread_id=%s LIMIT 1", (thread,))
    assert query("SELECT conversation_id FROM agent_task_usage")[0][0] is None
    url = urlsplit(service.backend.dsn)
    params = dict(parse_qsl(url.query))
    params["options"] = "-csearch_path=" + query("SELECT current_schema()")[0][0]
    scoped = urlunsplit((url.scheme, url.netloc, url.path, urlencode(params), url.fragment))
    monkeypatch.setattr(settings, "checkpoint_dsn", scoped)
    monkeypatch.setattr(settings, "checkpoint_forbid_db", "")
    from agent.scheduling.retention import collect

    with ExitStack() as resources:
        report = collect(
            service.backend,
            apply=True,
            now=lambda: utc_now() + timedelta(days=31),
            cleanup=Cleanup(service.backend.client, resources),
        )
    assert report["deleted"] == [root]
    assert query("SELECT count(*) FROM checkpoints WHERE thread_id=%s", (thread,))[0][0] == 0
    assert query("SELECT count(*) FROM agent_task_usage")[0][0] == 0
    assert query("SELECT count(*) FROM test_notifications")[0][0] == 1


def test_owned_schedule_lists_do_not_confuse_administrator_id_prefixes(site):
    service, query, _, _ = site
    query("INSERT INTO test_actors VALUES (70,'admin')")
    own, _, _ = create(service, delay_seconds=None, cron="0 8 * * *")
    other = "sc_" + uuid.uuid4().hex
    service.create(
        Principal(70, "admin", "assertion"),
        other,
        {
            "title": "other owner",
            "instruction": "private reminder",
            "action": "reminder",
            "cron": "0 8 * * *",
        },
    )
    assert [row["id"] for row in service.list(ADMIN)["items"]] == [own]
    assert [row["id"] for row in service.list(Principal(70, "admin", "assertion"))["items"]] == [
        other
    ]
    with pytest.raises(ScheduleNotFound):
        service.get(ADMIN, other)


def test_approved_publication_waits_durably_and_crash_replay_does_not_republish(site):
    service, query, start, processes = site
    query("UPDATE test_flags SET value=true WHERE key='block_publish_after_commit'")
    ident, _, _ = create(service, action="publish_announcement", title="审批标题",
                         instruction=" 审批正文\n😀 ", delay_seconds=5)
    assert service.backend.once(ADMIN.uid, ident).status == "DELAYED"
    assert query("SELECT count(*) FROM test_publications")[0][0] == 0
    eventually(lambda: query("SELECT count(*) FROM test_publications")[0][0] == 1)
    assert query("SELECT count(*) FROM test_notifications")[0][0] == 0
    processes[0].kill()
    processes[0].wait(timeout=10)
    query("UPDATE test_flags SET value=false WHERE key='block_publish_after_commit'")
    start()
    eventually(lambda: service.get(ADMIN, ident)["status"] == "finished")
    assert query("SELECT owner_id,title,body FROM test_publications") == [
        (7, "审批标题", " 审批正文\n😀 ")
    ]
    assert query("SELECT count(*) FROM test_notifications")[0][0] == 1
    result = service.runs(ADMIN, ident)["items"][0]["result"]
    assert result["status"] == "completed"
