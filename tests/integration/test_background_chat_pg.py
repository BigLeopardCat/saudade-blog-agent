"""Real DBOS streams survive detached readers and isolate concurrent conversations."""

import json
import os
import subprocess
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import psycopg
import pytest
from psycopg import sql

from agent.api.routes.chat_runs import Submission
from agent.background.service import STREAM, ChatRuns
from agent.principal import Principal
from agent.scheduling.backend import Backend

pytestmark = pytest.mark.enable_socket
USER = Principal(7, "user", "assertion")


def eventually(function):
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        result = function()
        if result:
            return result
        time.sleep(0.05)
    pytest.fail("background chat did not reach the expected state")


@pytest.fixture
def site(tmp_path):
    dsn = os.getenv("SAUDADE_SCHEDULING_TEST_DSN")
    if not dsn:
        pytest.skip("requires explicit disposable SAUDADE_SCHEDULING_TEST_DSN")
    schema = "chat_runs_" + uuid.uuid4().hex
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    release, requests = threading.Event(), []

    class Host(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            assert self.path == "/api/chat/runs/execute"
            assert self.headers["X-Chat-Run-Ticket"] == "synthetic-only"
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append(payload)
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            self.wfile.write(b'event: text\ndata: "started"\n\n')
            self.wfile.flush()
            if payload["message"] == "wait":
                release.wait(40)
            try:
                self.wfile.write(b'event: done\ndata: {}\n\n')
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Host)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    processes, logs = [], []

    def start():
        ready = tmp_path / (uuid.uuid4().hex + ".ready")
        log = ready.with_suffix(".log").open("w")
        logs.append(log)
        process = subprocess.Popen([sys.executable, "tests/integration/_background_chat_worker.py",
            dsn, schema, f"http://127.0.0.1:{server.server_port}", str(ready)],
            cwd=Path(__file__).resolve().parents[2], stdout=log, stderr=subprocess.STDOUT,
            env={**os.environ, "SAUDADE_IGNORE_ENV_FILE": "1", "PYTHONPATH": "."})
        processes.append(process)
        eventually(lambda: ready.exists() or process.poll() is not None)
        assert process.poll() is None, log.name

    backend = None
    try:
        start()
        backend = Backend(dsn, schema=schema)
        yield ChatRuns(backend), release, requests, processes, start
    finally:
        release.set()
        for process in processes:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=10)
        server.shutdown()
        server_thread.join(timeout=3)
        server.server_close()
        if backend:
            backend.close()
        for log in logs:
            log.close()
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


def submit(runs, cid, message):
    submission = Submission(run_id=uuid.uuid4().hex, conversation_id=cid,
        body=json.dumps({"conversation_id": cid, "message": message}), ticket="synthetic-only")
    runs.create(USER, submission)
    return submission.run_id


def test_detach_parallel_completion_and_offset_reconnect(site):
    runs, release, requests, _, _ = site
    first = submit(runs, 42, "wait")
    row = runs.owned(USER, first)
    reader = runs.client.read_stream(row.workflow_id, STREAM, timeout_seconds=15)
    assert next(reader) == {"event": "text", "data": "started"}
    reader.close()
    second = submit(runs, 43, "quick")
    eventually(lambda: runs.owned(USER, second).status == "SUCCESS")
    assert runs.owned(USER, first).status == "PENDING"
    release.set()
    eventually(lambda: runs.owned(USER, first).status == "SUCCESS")
    assert list(runs.client.read_stream(row.workflow_id, STREAM, offset=1)) == [
        {"event": "done", "data": {}}]
    assert len(requests) == 2


def test_worker_recovery_reports_interruption_without_replaying_host_request(site):
    runs, _, requests, processes, start = site
    identifier = submit(runs, 42, "wait")
    row = runs.owned(USER, identifier)
    reader = runs.client.read_stream(row.workflow_id, STREAM, timeout_seconds=15)
    assert next(reader)["event"] == "text"
    reader.close()
    processes[0].kill()
    processes[0].wait(timeout=10)
    start()
    eventually(lambda: runs.owned(USER, identifier).status == "SUCCESS")
    assert runs.owned(USER, identifier).output["status"] == "interrupted"
    events = list(runs.client.read_stream(row.workflow_id, STREAM))
    assert [item["event"] for item in events] == ["text", "error"]
    assert len(requests) == 1


def test_explicit_cancel_is_distinct_from_reader_detach(site):
    runs, _, requests, _, _ = site
    identifier = submit(runs, 42, "wait")
    eventually(lambda: bool(requests))
    assert runs.cancel(USER, identifier)["status"] == "CANCELLED"
    assert runs.owned(USER, identifier).status == "CANCELLED"
    assert len(requests) == 1
