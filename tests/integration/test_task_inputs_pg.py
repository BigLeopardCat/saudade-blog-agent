"""PostgreSQL inbox contract using an explicit disposable CI service."""

import os
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest

from agent.runtime.task_input_store import PostgresInputs, setup
from agent.runtime.task_inputs import InputConflict, TaskInputs

pytestmark = pytest.mark.enable_socket


@pytest.fixture
def inboxes():
    dsn = os.environ.get("SAUDADE_TASK_INPUT_TEST_DSN")
    if not dsn:
        pytest.skip("SAUDADE_TASK_INPUT_TEST_DSN not set; deployment DB is never consulted")
    import psycopg
    from psycopg import sql
    from psycopg.rows import dict_row
    from psycopg_pool import ConnectionPool

    schema = "input_test_" + uuid.uuid4().hex
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    try:
        with ConnectionPool(dsn, min_size=1, max_size=4, kwargs={"autocommit": True,
                "row_factory": dict_row, "options": "-c search_path=" + schema}) as pool:
            with pool.connection() as conn:
                setup(conn)
            yield TaskInputs(PostgresInputs(pool)), TaskInputs(PostgresInputs(pool))
    finally:
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


def test_two_workers_share_inputs_and_duplicate_delivery(inboxes):
    left, right = inboxes
    task = {"id": "one", "scope": {"uid": 7, "role": "user", "conversation_id": 42}}
    left.open(task)
    with ThreadPoolExecutor(max_workers=4) as executor:
        results = list(executor.map(
            lambda _: right.submit("one", task["scope"], "input", "要求"), range(4)))
    assert all(item == results[0] for item in results)
    assert len(left.read("one", task["scope"])["inputs"]) == 1
    left.finish(task, set(), paused=True)
    right.open(task, resume=True)
    assert len(right.boundary(task, set())) == 1
    right.boundary(task, {"input"}, applied={"input"}, final=True)
    assert left.read("one", task["scope"])["inputs"][0]["status"] == "applied"
    right.delete_conversation(42)
    with pytest.raises(InputConflict):
        left.read("one",task["scope"])


def test_finish_race_cannot_acknowledge_and_silently_skip_an_input(inboxes):
    left, right = inboxes
    for index in range(12):
        task = {"id": f"race-{index}", "scope": {"uid": 7, "role": "user", "conversation_id": 42}}
        left.open(task)
        gate = threading.Barrier(2)

        def submit():
            gate.wait()
            try:
                return right.submit(task["id"], task["scope"], "last", "late input")
            except InputConflict:
                return None

        def seal():
            gate.wait()
            return left.boundary(task, set(), final=True)

        with ThreadPoolExecutor(max_workers=2) as executor:
            incoming, finishing = executor.submit(submit), executor.submit(seal)
            item, pending = incoming.result(), finishing.result()
        assert (item is not None) == bool(pending)
        assert left.read(task["id"], task["scope"])["phase"] == (
            "running" if item else "finishing")
