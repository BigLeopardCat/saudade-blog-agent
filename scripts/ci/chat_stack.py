"""CI-only HTTP/1 widget→Nginx→Rust→Agent cancellation and checkpoint drill.

Only the model provider is synthetic. Business and checkpoint databases are
created for this invocation; no deployment configuration or process is used.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import ExitStack, contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

import httpx
import jwt
import psycopg
from psycopg import sql

AGENT = Path(__file__).resolve().parents[2]
MAIN_FINAL = "STACK_MAIN_FINAL"


class Provider(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self):
        super().__init__(("127.0.0.1", 0), ProviderHandler)
        self.lock = threading.Lock()
        self.entered, self.closed, self.calls = [], [], []

    def status(self):
        with self.lock:
            return {
                "entered": list(self.entered),
                "closed": list(self.closed),
                "calls": list(self.calls),
            }


class ProviderHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def do_GET(self):
        body = json.dumps(self.server.status()).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        assert self.path == "/v1/chat/completions"
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        history = json.dumps(body["messages"], ensure_ascii=False)
        marker = next(
            (tag for tag in ("STACK_PROBE", "STACK_DROP", "STACK_MAIN") if tag in history), None
        )
        assert marker, "Unexpected provider traffic; fixture must not answer unrelated requests"
        with self.server.lock:
            self.server.calls.append(marker)
        names = {row["function"]["name"] for row in body.get("tools", [])}
        completed_read = any(
            row.get("role") == "tool" and row.get("tool_call_id") == "clock"
            for row in body["messages"]
        )
        if marker == "STACK_MAIN" and not completed_read:
            name = (
                "get_current_time" if "get_current_time" in names else "load_utilities_info_tools"
            )
            assert name in names, "Required public tool is missing"
            message = {
                "tool_calls": [
                    {
                        "id": "clock" if name == "get_current_time" else "load",
                        "type": "function",
                        "function": {"name": name, "arguments": "{}"},
                    }
                ]
            }
            self.reply(body, message)
            return
        with self.server.lock:
            hold = marker != "STACK_PROBE" and marker not in self.server.entered
            if hold:
                self.server.entered.append(marker)
        if hold:
            assert body.get("stream"), "This acceptance case requires real model streaming"
            self.reply(body, {"content": marker + "_PARTIAL"}, hold=True)
            self.connection.settimeout(15)
            try:
                disconnected = self.rfile.read(1) == b""
            except OSError:
                disconnected = False
            if disconnected:
                with self.server.lock:
                    self.server.closed.append(marker)
            return
        self.reply(body, {"content": MAIN_FINAL if marker == "STACK_MAIN" else "STACK_PROBE_FINAL"})

    def reply(self, request, message, *, hold=False):
        envelope = {"id": "synthetic-stack", "created": 0, "model": "gpt-4o-mini"}
        usage = {"prompt_tokens": 12, "completion_tokens": 3, "total_tokens": 15}
        reason = "tool_calls" if message.get("tool_calls") else "stop"
        if not request.get("stream"):
            body = json.dumps(
                {
                    **envelope,
                    "object": "chat.completion",
                    "usage": usage,
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", **message},
                            "finish_reason": reason,
                        }
                    ],
                }
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        delta = {"role": "assistant", **message}
        if delta.get("tool_calls"):
            delta["tool_calls"] = [
                {"index": index, **call} for index, call in enumerate(delta["tool_calls"])
            ]
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(
            (
                "data: "
                + json.dumps(
                    {
                        **envelope,
                        "object": "chat.completion.chunk",
                        "choices": [{"index": 0, "delta": delta, "finish_reason": None}],
                    }
                )
                + "\n\n"
            ).encode()
        )
        if not hold:
            self.wfile.write(
                (
                    "data: "
                    + json.dumps(
                        {
                            **envelope,
                            "object": "chat.completion.chunk",
                            "choices": [{"index": 0, "delta": {}, "finish_reason": reason}],
                        }
                    )
                    + "\n\n"
                ).encode()
            )
            self.wfile.write(
                (
                    "data: "
                    + json.dumps(
                        {
                            **envelope,
                            "object": "chat.completion.chunk",
                            "choices": [],
                            "usage": usage,
                        }
                    )
                    + "\n\ndata: [DONE]\n\n"
                ).encode()
            )
        self.wfile.flush()


@contextmanager
def databases(admin_dsn):
    parts = urlsplit(admin_dsn)
    if parts.scheme not in ("postgres", "postgresql") or parts.hostname not in (
        "localhost",
        "127.0.0.1",
    ):
        raise ValueError("Explicit loopback CI PostgreSQL DSN required")
    if not parts.username or not parts.password:
        raise ValueError("Explicit disposable CI database credentials required")
    names = ["chat_stack_blog_" + secrets.token_hex(6), "chat_stack_agent_" + secrets.token_hex(6)]
    created = []
    with psycopg.connect(admin_dsn, autocommit=True, connect_timeout=5) as connection:
        try:
            for name in names:
                connection.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
                created.append(name)
            yield [parts._replace(path="/" + name).geturl() for name in names]
        finally:
            for name in reversed(created):
                connection.execute(
                    sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name))
                )


def child(stack, command, cwd, env, log):
    output = stack.enter_context(log.open("w"))
    process = subprocess.Popen(command, cwd=cwd, env=env, stdout=output, stderr=subprocess.STDOUT)

    def stop():
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=8)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)

    stack.callback(stop)
    return process


def wait_ready(url, process):
    with httpx.Client(trust_env=False, timeout=1) as client:
        for _ in range(100):
            if process.poll() is not None:
                raise RuntimeError(
                    "Fixture service exited during startup; inspect its synthetic log"
                )
            try:
                if client.get(url).status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            time.sleep(0.2)
    raise TimeoutError("Fixture service readiness timed out")


def verify_persistence(blog_dsn, agent_dsn, result):
    from langgraph.checkpoint.postgres import PostgresSaver

    from agent.runtime.checkpoint_conversations import CheckpointConversationStore

    main = result["main"]
    with psycopg.connect(blog_dsn) as connection:
        rows = connection.execute(
            "SELECT role,content FROM chat_history WHERE conversation_id=%s "
            "AND turn_id=%s ORDER BY id",
            (main["conversation_id"], main["turn_id"]),
        ).fetchall()
    assert [role for role, _ in rows] == ["user", "assistant"], (
        "Original message or final reply duplicated"
    )
    assert MAIN_FINAL in rows[1][1]
    with PostgresSaver.from_conn_string(agent_dsn) as saver:
        saved = CheckpointConversationStore(saver).load(
            {"configurable": {"thread_id": "conv_" + str(main["conversation_id"])}}
        )
        assert saved.initialized and not saved.unfinished and saved.last_outcome == "responded"
        assert sum(message.type == "ai" and MAIN_FINAL in str(message.content)
                   for message in saved.messages) == 1


def run(args):
    if os.environ.get("CI") != "true" or not os.environ.get("CHAT_STACK_PG_ADMIN_DSN"):
        raise RuntimeError("CI=true and explicit CHAT_STACK_PG_ADMIN_DSN are required")
    out = Path(os.environ["CHAT_STACK_ARTIFACT_DIR"]).resolve()
    out.mkdir(parents=True, exist_ok=True)
    blog = args.blog.resolve()
    binary = args.backend.resolve()
    widget = blog / "frontend" / "public"
    assert (widget / "live2d-widgets" / "boot.js").is_file(), "Run the host fetch:widget first"
    provenance = json.loads(binary.with_name("provenance.json").read_text())
    assert hashlib.sha256(binary.read_bytes()).hexdigest() == provenance["sha256"]
    assert (
        subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=blog, text=True).strip()
        == provenance["tested_merge"]
    )
    with tempfile.TemporaryDirectory(prefix="chat-stack-") as temporary, ExitStack() as stack:
        work = Path(temporary)
        blog_dsn, agent_dsn = stack.enter_context(databases(os.environ["CHAT_STACK_PG_ADMIN_DSN"]))
        files = subprocess.check_output(
            [sys.executable, "scripts/migration/pg/ci_schema_files.py"], cwd=blog, text=True
        ).splitlines()
        parts = urlsplit(blog_dsn)
        sql_env = {
            **os.environ,
            "PGHOST": parts.hostname,
            "PGPORT": str(parts.port or 5432),
            "PGUSER": parts.username,
            "PGPASSWORD": parts.password,
            "PGDATABASE": parts.path[1:],
            "PGCONNECT_TIMEOUT": "5",
        }
        for name in files:
            subprocess.run(
                ["psql", "-X", "-q", "-v", "ON_ERROR_STOP=1", "-f", str(blog / name)],
                env=sql_env,
                check=True,
                stdout=subprocess.DEVNULL,
                timeout=30,
            )
        with psycopg.connect(blog_dsn, autocommit=True) as connection:
            uid = connection.execute(
                'INSERT INTO "user" (username,password,role) '
                "VALUES (%s,'fixture-unused','user') RETURNING id",
                ("stack_" + secrets.token_hex(6),),
            ).fetchone()[0]
        provider = Provider()
        provider_thread = threading.Thread(target=provider.serve_forever, daemon=True)
        provider_thread.start()
        stack.callback(provider.server_close)
        stack.callback(provider.shutdown)
        stack.callback(lambda: (out / "provider.json").write_text(json.dumps(provider.status())))
        common = {
            key: os.environ[key]
            for key in ("PATH", "LANG", "LC_ALL", "VIRTUAL_ENV")
            if key in os.environ
        }
        assertion, session_key = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        rust_env = {
            **common,
            "DATABASE_URL": blog_dsn,
            "BLOG_PORT": "18081",
            "JWT_SECRET": session_key,
            "AGENT_ASSERTION_SECRET": assertion,
            "BLOG_DELEGATION_SECRET": secrets.token_urlsafe(32),
            "AGENT_URL": "http://127.0.0.1:18082/chat",
            "UPLOAD_DIR": str(work / "uploads"),
        }
        backend = child(stack, [str(binary)], work, rust_env, out / "rust.log")
        wait_ready("http://127.0.0.1:18081/api/public/activity/announcements?page_size=1", backend)
        agent_env = {
            **common,
            "PYTHONPATH": str(AGENT),
            "SAUDADE_IGNORE_ENV_FILE": "1",
            "LLM_PROVIDER": "openai",
            "OPENAI_MODEL": "gpt-4o-mini",
            "OPENAI_API_KEY": "synthetic-only",
            "OPENAI_BASE_URL": f"http://127.0.0.1:{provider.server_port}/v1",
            "LLM_ENABLE_THINKING": "0",
            "AGENT_ASSERTION_SECRET": assertion,
            "AGENT_CONFIRM_SECRET": secrets.token_urlsafe(32),
            "CHECKPOINT_DSN": agent_dsn,
            "CHECKPOINT_FORBID_DB": parts.path[1:],
            "AGENT_MAX_CONCURRENT": "1",
            "AGENT_STREAM_IDLE_TIMEOUT": "15",
            "AGENT_STREAM_TOTAL_TIMEOUT": "45",
            "BLOG_API_BASE": "http://127.0.0.1:18081/api/public",
            "GRAPH_API_BASE": "http://127.0.0.1:18081/api/public",
            "AGENT_ADMIN_BASE": "http://127.0.0.1:18081",
            "SCHEDULING_ENABLED": "0",
            "IOT_ENABLED": "0",
            "RAG_HYBRID_ENABLED": "0",
            "TRACE_DIR": str(work / "traces"),
            "DELEGATION_DIR": str(work / "delegations"),
            "RAG_VECTOR_DIR": str(work / "vectors"),
        }
        agent = child(
            stack,
            [
                sys.executable,
                "-m",
                "uvicorn",
                "agent.main:app",
                "--host",
                "127.0.0.1",
                "--port",
                "18082",
            ],
            work,
            agent_env,
            out / "agent.log",
        )
        wait_ready("http://127.0.0.1:18082/ready", agent)
        agent_head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=AGENT, text=True).strip()
        with httpx.Client(trust_env=False, timeout=2) as client:
            assert client.get("http://127.0.0.1:18082/version").json()["sha"] == agent_head
        config = work / "nginx.conf"
        config.write_text(f"""pid {work}/nginx.pid;
error_log {out}/nginx-error.log;
events {{ worker_connections 64; }}
http {{
  include /etc/nginx/mime.types;
  client_body_temp_path {work}/client_temp;
  proxy_temp_path {work}/proxy_temp;
  fastcgi_temp_path {work}/fastcgi_temp;
  uwsgi_temp_path {work}/uwsgi_temp;
  scgi_temp_path {work}/scgi_temp;
  access_log {out}/nginx-access.log;
  server {{
    listen 127.0.0.1:18080;
    root {widget};
    location = / {{ alias {AGENT}/frontend/tests/fixtures/chat-bootstrap.html; }}
    location ^~ /api/ {{
      proxy_pass http://127.0.0.1:18081;
      proxy_http_version 1.1;
      proxy_set_header Host $host;
      proxy_set_header X-Forwarded-Proto $scheme;
      proxy_read_timeout 300s;
      proxy_send_timeout 300s;
      proxy_connect_timeout 10s;
    }}
  }}
}}
""")
        nginx = child(
            stack,
            ["nginx", "-p", str(work), "-c", str(config), "-g", "daemon off;"],
            work,
            common,
            out / "nginx.log",
        )
        wait_ready("http://127.0.0.1:18080/", nginx)
        token = jwt.encode(
            {
                "sub": uid,
                "role": "user",
                "ver": 0,
                "aud": "blog-session",
                "exp": int(time.time()) + 300,
            },
            session_key,
            algorithm="HS256",
        )
        result_file = work / "browser-result.json"
        node_env = {
            **common,
            "CHAT_STACK_TOKEN": token,
            "CHAT_STACK_UID": str(uid),
            "CHAT_STACK_ORIGIN": "http://127.0.0.1:18080",
            "CHAT_STACK_PROVIDER": f"http://127.0.0.1:{provider.server_port}",
            "CHAT_STACK_RESULT_FILE": str(result_file),
            "CHAT_STACK_ARTIFACT_DIR": str(out),
        }
        subprocess.run(
            ["node", "frontend/tests/chat-stack.browser.mjs"],
            cwd=AGENT,
            env=node_env,
            check=True,
            timeout=90,
        )
        result = json.loads(result_file.read_text())
        verify_persistence(blog_dsn, agent_dsn, result)
        deadline = time.monotonic() + 5
        events = []
        while time.monotonic() < deadline:
            records = [json.loads(path.read_text()) for path in (work / "traces").rglob("*.json")]
            events = [event for trace in records for event in trace.get("events", [])]
            if sum(event.get("event") == "jobs_drained" for event in events) >= 4:
                break
            time.sleep(0.05)
        reads = [
            event
            for event in events
            if event.get("event") == "call" and event.get("name") == "get_current_time"
        ]
        assert len(reads) == 1, "The completed read was replayed"
        assert sum(event.get("event") == "jobs_drained" for event in events) >= 4
        status = provider.status()
        assert set(status["closed"]) == {"STACK_MAIN", "STACK_DROP"}
        (out / "summary.json").write_text(
            json.dumps(
                {
                    "browser": result,
                    "provider": status,
                    "completed_read_calls": len(reads),
                    "persisted_main_terminal": True,
                    "agent_head": agent_head,
                    "backend_provenance": provenance,
                    "transport": "HTTP/1 real stack with synthetic provider",
                },
                indent=2,
            )
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--blog", type=Path, required=True)
    parser.add_argument("--backend", type=Path, required=True)
    os.environ["SAUDADE_IGNORE_ENV_FILE"] = "1"
    run(parser.parse_args())
