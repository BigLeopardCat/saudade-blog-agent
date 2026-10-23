"""Framework types select durable behavior; schema setup belongs to startup."""

from contextlib import contextmanager, nullcontext
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.postgres import PostgresSaver

from agent import checkpoint, idem, idem_pg, runtime_probe
from agent.runtime import checkpoint_store, turns, writes


class ExtendedPostgresSaver(PostgresSaver):
    pass


def test_postgres_extension_uses_the_shared_lock_and_backend_contract(monkeypatch):
    saver = ExtendedPostgresSaver(None)
    assert checkpoint_store.postgres(saver)
    assert runtime_probe.checkpoint_backend(SimpleNamespace(checkpointer=saver)) == "postgres"
    events = []

    @contextmanager
    def lease(actual, thread, config, deadline):
        assert actual is saver and thread == "contract-thread"
        events.append("locked")
        try:
            yield
        finally:
            events.append("unlocked")

    monkeypatch.setattr(turns, "_postgres_turn", lease)
    monkeypatch.setattr(checkpoint_store, "require_active",
                        lambda *args: events.append("checked"))
    with turns.conversation_turn(saver, {"configurable": {"thread_id": "contract-thread"}}):
        events.append("turn")
    assert events == ["locked", "checked", "turn", "unlocked"]


def test_module_name_cannot_make_an_object_a_postgres_store():
    impostor = type("Impostor", (), {"__module__": "langgraph.checkpoint.postgres"})()
    assert not checkpoint_store.postgres(impostor)
    assert checkpoint_store.backend(impostor) != "postgres"
    assert checkpoint_store.backend(InMemorySaver()) == "memory"
    with pytest.raises(RuntimeError, match="PostgreSQL"):
        writes.ledger_for(impostor)


def test_startup_initializes_the_ledger_once_without_extra_pool_slots(monkeypatch):
    connection = Mock(autocommit=True)
    pool = Mock()
    pool.connection.side_effect = lambda: nullcontext(connection)
    saver = ExtendedPostgresSaver(pool)
    monkeypatch.setattr(saver, "setup", lambda: None)
    monkeypatch.setattr(checkpoint, "wait_for_setup_lock", lambda *a, **kw: None)
    monkeypatch.setattr(checkpoint, "drop_invalid_indexes", lambda *a: None)
    checkpoint._setup_under_lock(saver, pool=pool)
    assert pool.connection.call_count == 1
    statements = [str(call.args[0]) for call in connection.execute.call_args_list]
    assert sum("CREATE TABLE IF NOT EXISTS agent_write_idem" in s for s in statements) == 1
    assert "pg_advisory_unlock" in statements[-1]
    before = connection.execute.call_count
    for _ in range(3):
        assert isinstance(writes.ledger_for(saver), idem_pg.PostgresLedger)
        cfg = {"configurable": {"__pregel_checkpointer": saver}}
        assert isinstance(idem._shared(cfg), idem_pg.PostgresLedger)
    assert connection.execute.call_count == before
    assert pool.connection.call_count == 1


def test_failed_ledger_setup_aborts_startup_and_releases_setup_lock(monkeypatch):
    connection = Mock(autocommit=True)

    def execute(statement, *args):
        if "agent_write_idem" in statement:
            raise RuntimeError("schema unavailable")

    connection.execute.side_effect = execute
    pool = Mock()
    pool.connection.side_effect = lambda: nullcontext(connection)
    saver = ExtendedPostgresSaver(pool)
    monkeypatch.setattr(saver, "setup", lambda: None)
    monkeypatch.setattr(checkpoint, "wait_for_setup_lock", lambda *a, **kw: None)
    monkeypatch.setattr(checkpoint, "drop_invalid_indexes", lambda *a: None)
    with pytest.raises(RuntimeError, match="schema unavailable"):
        checkpoint._setup_under_lock(saver, pool=pool)
    assert "pg_advisory_unlock" in connection.execute.call_args.args[0]


def test_invalid_pool_configuration_never_echoes_credentials():
    credential_value = "PRIVATE_CREDENTIAL"
    pool = SimpleNamespace(kwargs={"autocommit": False, "password": credential_value})
    with pytest.raises(RuntimeError, match="autocommit") as error:
        idem_pg.ensure_autocommit(pool)
    assert "PRIVATE_CREDENTIAL" not in str(error.value)


def test_legacy_postgres_store_cannot_silently_fall_back_to_sqlite():
    cfg = {"configurable": {"__pregel_checkpointer": ExtendedPostgresSaver(None)}}
    with pytest.raises(RuntimeError, match="不能回落"):
        idem._pg_pool(cfg)
    unrelated = SimpleNamespace(conn=SimpleNamespace(connection=lambda: None))
    with pytest.raises(RuntimeError, match="不支持"):
        idem._pg_pool({"configurable": {"__pregel_checkpointer": unrelated}})
