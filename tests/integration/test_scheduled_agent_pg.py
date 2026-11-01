"""Background Agent recovery on a real saver, without model or business network calls."""

import os
import uuid
from contextlib import ExitStack
from types import SimpleNamespace
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import psycopg
import pytest
from langchain_core.messages import AIMessageChunk
from langchain_core.tools import tool
from psycopg import sql

from agent.principal import Principal
from agent.runtime.control import AgentCancelled
from agent.scheduling import readonly
from agent.scheduling.checkpoints import remove_run
from config.settings import settings


def test_background_agent_restores_observations_and_budget_on_real_checkpoint(monkeypatch):
    dsn = os.getenv("SAUDADE_SCHEDULING_TEST_DSN")
    if not dsn:
        pytest.skip("requires explicit disposable SAUDADE_SCHEDULING_TEST_DSN")
    schema = "background_" + uuid.uuid4().hex
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    url = urlsplit(dsn)
    params = dict(parse_qsl(url.query))
    params["options"] = "-csearch_path=" + schema
    scoped = urlunsplit((url.scheme, url.netloc, url.path, urlencode(params), url.fragment))
    monkeypatch.setattr(settings, "checkpoint_dsn", scoped)
    monkeypatch.setattr(settings, "checkpoint_forbid_db", "")
    reads = []
    options = []

    @tool("get_current_time")
    def read_record() -> str:
        """Read one isolated test fact."""
        reads.append("read")
        return "fixture fact"

    class Model:
        max_tokens = 64

        def __init__(self, interrupt):
            self.calls = 0
            self.interrupt = interrupt

        def bind_tools(self, tools, **kwargs):
            return self

        def stream(self, messages, **kwargs):
            self.calls += 1
            if self.interrupt and self.calls == 2:
                raise AgentCancelled()
            if self.interrupt:
                yield AIMessageChunk(
                    content="",
                    tool_calls=[{"name": "get_current_time", "args": {}, "id": "read-once"}],
                )
            else:
                yield AIMessageChunk(content="已核对隔离记录")

    catalog = SimpleNamespace(
        initial_tools=lambda _: {"get_current_time": read_record}, domains=lambda *_, **__: {}
    )
    actor = Principal(7, "admin", "assertion")
    run_id = "owned-background-fixture"
    try:
        # Exercise upgrade of an already-installed usage table, not just a fresh CREATE.
        with psycopg.connect(scoped) as conn:
            conn.execute(
                "CREATE TABLE agent_task_usage(task_id text primary key,"
                "conversation_id bigint NOT NULL,scope jsonb NOT NULL,spent jsonb NOT NULL)"
            )
        first_model = Model(True)

        def first_factory(**kwargs):
            options.append(kwargs)
            return first_model

        monkeypatch.setattr(readonly, "get_llm", first_factory)
        with ExitStack() as resources:
            first = readonly.ReadOnlyRunner(resources)
            first.runtime.application.catalog = catalog
            with pytest.raises(AgentCancelled):
                first.run(actor, run_id, "核对隔离记录")
        with psycopg.connect(scoped) as conn:
            row = conn.execute(
                "SELECT conversation_id,scope,spent FROM agent_task_usage"
            ).fetchone()
            assert row[0] is None and row[1]["background_run_id"] == run_id
            assert row[2]["tool_calls"] == 1 and row[2]["model_calls"] >= 2
        monkeypatch.setattr(readonly, "get_llm", lambda **kw: Model(False))
        with ExitStack() as resources:
            recovered = readonly.ReadOnlyRunner(resources)
            recovered.runtime.application.catalog = catalog
            result = recovered.run(actor, run_id, "核对隔离记录")
            assert result["text"] == "已核对隔离记录"
            assert result["spent"]["tool_calls"] == 1
            assert result["spent"]["model_calls"] >= 3
            assert recovered.run(actor, run_id, "核对隔离记录")["reused"]
            remove_run(recovered.runtime.saver, actor.uid, run_id)
            remove_run(recovered.runtime.saver, actor.uid, run_id)
            assert not recovered.runtime.store.load(
                {"configurable": {"thread_id": result["thread_id"]}}
            ).initialized
            with psycopg.connect(scoped) as conn:
                assert conn.execute("SELECT count(*) FROM agent_task_usage").fetchone()[0] == 0
        assert reads == ["read"] and options[0]["max_tokens"] == 1000
    finally:
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))
