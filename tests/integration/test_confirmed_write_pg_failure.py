"""Real HTTP effect plus PostgreSQL ledger failure; not a Rust/proxy end-to-end drill."""

import json
import os
import secrets
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import jwt
import pytest

from agent import confirm, delegation, idem_pg
from agent.domains.favorites import transactions
from agent.principal import SOURCE_ASSERTION, Principal
from agent.runtime import writes
from agent.runtime.turns import AgentCancelled
from tools import base, own_data

pytestmark = pytest.mark.enable_socket


@pytest.fixture
def fault_site(monkeypatch):
    dsn = os.environ.get("SAUDADE_TASK_INPUT_TEST_DSN")
    if not dsn:
        if os.environ.get("SAUDADE_REQUIRE_CKPT_TEST") == "1":
            pytest.fail("explicit disposable PostgreSQL DSN is required by CI")
        pytest.skip("SAUDADE_TASK_INPUT_TEST_DSN not set; deployment settings are never read")
    import psycopg
    from psycopg import sql
    from psycopg_pool import ConnectionPool

    schema = "write_fault_" + uuid.uuid4().hex
    application = schema + "_ledger"
    options = f"-c search_path={schema} -c statement_timeout=5000"
    state = {"requests": 0, "failure": None, "terminated": False}
    with psycopg.connect(dsn, autocommit=True, connect_timeout=3) as admin:
        admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    try:
        with psycopg.connect(dsn, autocommit=True, options=options, connect_timeout=3) as admin:
            admin.execute("CREATE TABLE effect(owner_id integer NOT NULL, "
                          "article_id integer NOT NULL)")
        with ConnectionPool(dsn, min_size=1, max_size=1, timeout=3, kwargs={
            "autocommit": True, "options": options, "application_name": application,
            "connect_timeout": 3,
        }) as pool:
            pool.wait(timeout=3)
            idem_pg.setup(pool)
            with pool.connection() as conn:
                state["pid"] = conn.execute("SELECT pg_backend_pid()").fetchone()[0]
            state["ledger"] = idem_pg.PostgresLedger(pool)
            host_key = secrets.token_urlsafe(32)
            capability = jwt.encode({"sub": 7, "exp": int(time.time()) + 120, "ver": 1,
                "role": "user", "aud": "blog-tools", "scope": "tools"}, host_key,
                algorithm="HS256")
            context = delegation.clear()
            delegation.accept(7, capability)

            class Handler(BaseHTTPRequestHandler):
                def log_message(self, *args):
                    pass

                def do_POST(self):
                    state["requests"] += 1
                    try:
                        assert self.path == "/api/protected/favorites"
                        token = self.headers["Authorization"].removeprefix("Bearer ")
                        claims = jwt.decode(token, host_key, algorithms=["HS256"],
                            audience="blog-tools", options={"verify_sub": False})
                        assert claims["sub"] == 7 and claims["scope"] == "tools"
                        payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                        assert payload == {"noteId": 19}
                        with psycopg.connect(dsn, autocommit=True, options=options,
                                             connect_timeout=3) as effect:
                            assert effect.execute(
                                "SELECT status FROM agent_write_idem WHERE key=%s",
                                (state["key"],)).fetchone() == ("intent",)
                            effect.execute("INSERT INTO effect VALUES(%s,%s)", (7, 19))
                            # Only terminate the fresh ledger connection owned by this fixture.
                            terminated = effect.execute(
                                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                                "WHERE pid=%s AND application_name=%s AND usename=current_user "
                                "AND datname=current_database() AND pid<>pg_backend_pid()",
                                (state["pid"], application)).fetchone()
                            assert terminated == (True,)
                            state["terminated"] = True
                        body = {"code": 200, "data": {"ownerUserId": 7, "noteId": 19,
                            "isFavorite": True, "changed": True}}
                        status = 200
                    except Exception as failure:
                        state["failure"] = failure
                        body, status = {"code": 500, "message": "synthetic effect failed"}, 500
                    encoded = json.dumps(body).encode()
                    self.send_response(status)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(encoded)))
                    self.end_headers()
                    self.wfile.write(encoded)

            server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
            worker = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05},
                                      daemon=True)
            worker.start()
            try:
                with httpx.Client(trust_env=False, timeout=3) as client:
                    monkeypatch.setattr(base, "_client", client)
                    monkeypatch.setattr(base, "ADMIN_BASE", f"http://127.0.0.1:{server.server_port}")
                    monkeypatch.setattr(confirm.settings, "confirmation_secret",
                                        secrets.token_urlsafe(32))
                    yield state, dsn, options
            finally:
                server.shutdown()
                server.server_close()
                worker.join(timeout=3)
                delegation.reset(context)
    finally:
        with psycopg.connect(dsn, autocommit=True, connect_timeout=3) as admin:
            admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


def test_committed_http_effect_survives_lost_ledger_connection_without_blind_replay(fault_site):
    import psycopg
    from psycopg_pool import ConnectionPool

    site, dsn, options = fault_site
    config = {"configurable": {"thread_id": "conv_42", "conversation_id": 42, "user_id": 7,
        "principal": Principal(7, "user", SOURCE_ASSERTION)}}
    token = confirm.sign(7, 42, "favorite_add",
        [{"tool": "add_favorite", "args": {"article_id": 19}}], executor="favorites")
    grant = confirm.verify(token, 7, 42)
    assert grant is not None
    tools = {"add_favorite": own_data.add_favorite}
    wrong_scope = {"configurable": {**config["configurable"], "user_id": 8,
        "principal": Principal(8, "user", SOURCE_ASSERTION)}}
    denied = transactions.execute(grant, wrong_scope, None, tools=tools, ledger=site["ledger"])
    assert denied["blocked"] and not denied["receipts"]
    stopped = threading.Event()
    stopped.set()
    with pytest.raises(AgentCancelled):
        transactions.execute(grant, {"configurable": {**config["configurable"],
            "stop_event": stopped}}, None, tools=tools, ledger=site["ledger"])
    name, args = transactions.validate(grant, config)
    key = writes.operation_key(grant, name, args)
    site["key"] = key
    assert site["ledger"].lookup(key) is None and site["requests"] == 0

    # The actual HTTP tool receives its typed success, then the production finish SQL fails.
    with pytest.raises(psycopg.OperationalError):
        writes.execute_once(grant, name, args, config, site["ledger"], tools)
    assert site["failure"] is None and site["terminated"] and site["requests"] == 1
    site["ledger"].pool.close()
    with ConnectionPool(dsn, min_size=1, max_size=1, timeout=3,
            kwargs={"autocommit": True, "options": options, "connect_timeout": 3}) as fresh:
        fresh.wait(timeout=3)
        rebuilt = idem_pg.PostgresLedger(fresh)
        with fresh.connection() as conn:
            assert conn.execute("SELECT pg_backend_pid()").fetchone()[0] != site["pid"]
            assert conn.execute("SELECT owner_id,article_id FROM effect").fetchall() == [(7, 19)]
        for _ in range(2):
            replay = transactions.execute(grant, config, None, tools=tools, ledger=rebuilt)
            assert replay["receipts"] == [] and replay["blocked"]
            assert replay["operation_outcome"]["status"] == "unverified"
            assert rebuilt.lookup(key)[0] == "intent"
        with fresh.connection() as conn:
            assert conn.execute("SELECT count(*) FROM effect").fetchone()[0] == 1
        assert site["requests"] == 1
