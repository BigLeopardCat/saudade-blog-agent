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
                from agent.runtime import task_budget
                task_budget.setup(conn)
            yield TaskInputs(PostgresInputs(pool)), TaskInputs(PostgresInputs(pool))
    finally:
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


def test_task_usage_survives_worker_reconstruction_and_checks_source(inboxes):
    from langchain_core.messages import HumanMessage

    from agent.runtime.budget import BudgetExceeded, BudgetLimits, RequestBudget
    from agent.runtime.task_budget import PostgresUsage

    left, right = [PostgresUsage(inbox.storage.pool) for inbox in inboxes]
    task = {"id": "budget", "scope": {"uid": 7, "role": "user", "conversation_id": 42}}
    budget = RequestBudget(BudgetLimits(model_calls=1))
    budget.bind_task(left, task)
    budget.reserve([HumanMessage(content="synthetic")], output_limit=64)
    restored = RequestBudget(BudgetLimits(model_calls=1))
    restored.bind_task(right, task)
    assert restored.model_calls == 1 and restored.tokens == budget.tokens
    assert restored.request_spent()["model_calls"] == 0
    with pytest.raises(BudgetExceeded, match="model_calls"):
        restored.reserve([HumanMessage(content="retry")], output_limit=64)
    with pytest.raises(ValueError, match="scope"):
        right.load({**task, "scope": {**task["scope"], "uid": 8}})
    right.delete_conversation(42)
    assert left.load(task) == {}


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


@pytest.mark.parametrize("fail_commit", [False, True])
def test_framework_turn_commit_survives_saver_reconstruction(inboxes, monkeypatch, fail_commit):
    from _own_support import config
    from langchain_core.messages import AIMessage, HumanMessage
    from langgraph.checkpoint.postgres import PostgresSaver

    from agent.runtime import checkpoint_store, turn_commit
    from agent.runtime.native import NativeRuntime
    from agent.runtime.native_store import NativeConversationStore

    pool = inboxes[0].storage.pool
    PostgresSaver(pool).setup()
    with pool.connection() as conn:
        checkpoint_store.setup(conn)
    calls = []

    class Model:
        max_tokens = 128

        def bind_tools(self, *args, **kwargs):
            return self

        def invoke(self, messages, **kwargs):
            calls.append(messages)
            assert len(calls) == 1, "reconstruction repeated the completed model"
            return AIMessage(content="已核实的测试答复。")

    def runtime():
        return NativeRuntime(NativeConversationStore(PostgresSaver(pool)),
                             model_factory=lambda **_: Model())

    original = turn_commit.commit
    if fail_commit:
        def fail(*args, **kwargs):
            raise OSError("commit fixture interruption")

        monkeypatch.setattr(turn_commit, "commit", fail)
    selected = runtime()
    inputs = {"messages": [HumanMessage(content="回答一次")], "task_id": "commit-task"}
    if fail_commit:
        with pytest.raises(OSError, match="commit fixture interruption"):
            list(selected.stream_events(inputs, config()))
        assert selected.store.load(config()).unfinished
        assert selected.store.load(config()).messages == []
        monkeypatch.setattr(turn_commit, "commit", original)
        result = list(runtime().stream_events({"resume_task": "commit-task"}, config()))[-1]
    else:
        result = list(selected.stream_events(inputs, config()))[-1]
    rebuilt = runtime()
    saved = rebuilt.store.load(config())
    assert result.reply == "已核实的测试答复。" and len(calls) == 1
    assert [m.content for m in saved.messages] == ["回答一次", result.reply]
    assert not saved.unfinished and rebuilt.get_state(config()).values["turn"] is None


def test_browser_ack_then_disconnect_recovers_the_actual_child_checkpoint(inboxes, monkeypatch):
    from _browser_support import Model, acknowledge
    from _own_support import config
    from langchain_core.messages import HumanMessage, ToolMessage
    from langgraph.checkpoint.postgres import PostgresSaver

    from agent.runtime import checkpoint_store
    from agent.runtime.control import AgentCancelled
    from agent.runtime.native import NativeRuntime
    from agent.runtime.native_store import NativeConversationStore

    pool = inboxes[0].storage.pool
    PostgresSaver(pool).setup()
    with pool.connection() as conn:
        checkpoint_store.setup(conn)
    model, cfg = Model(), config(browser_feedback=True)

    def runtime():
        return NativeRuntime(NativeConversationStore(PostgresSaver(pool)),
                             model_factory=lambda **_: model)

    first = list(runtime().stream_events({"messages": [HumanMessage(content="去留言板")],
                                         "task_id": "page-task"}, cfg))[-1]
    original = model.invoke

    def disconnected(messages, **kwargs):
        raise AgentCancelled()

    with monkeypatch.context() as scoped:
        scoped.setattr(model, "invoke", disconnected)
        with pytest.raises(AgentCancelled):
            list(runtime().stream_events({"resume_task": "page-task"},
                acknowledge(cfg, first.execution["client_action"])))
    assert model.invoke == original and runtime().store.load(cfg).unfinished
    result = list(runtime().stream_events({"resume_task": "page-task"}, cfg))[-1]
    assert result.reply and not result.execution.get("client_action")
    assert sum(isinstance(m, ToolMessage) and m.name == "navigate_page"
               for m in model.calls[-1]) == 1
    assert not runtime().store.load(cfg).unfinished


@pytest.mark.parametrize("phase", ["commit", "feedback"])
def test_actual_process_exit_recovers_durable_task_once(inboxes, tmp_path, monkeypatch, phase):
    import secrets
    import subprocess
    import sys
    from pathlib import Path

    from _own_support import config
    from langchain_core.messages import AIMessage, HumanMessage
    from langgraph.checkpoint.postgres import PostgresSaver
    from test_confirmation_continuation import Model

    from agent import confirm, idem_pg
    from agent.runtime import checkpoint_store, writes
    from agent.runtime.native import NativeRuntime
    from agent.runtime.native_store import NativeConversationStore
    from eval.own_fixtures import favorite_backend

    pool = inboxes[0].storage.pool
    PostgresSaver(pool).setup()
    with pool.connection() as conn:
        checkpoint_store.setup(conn)
        idem_pg.initialize(conn)
    secret = secrets.token_urlsafe(32)
    root = Path(__file__).resolve().parents[2]
    marker = tmp_path / "revocation-marker"
    env = {"PATH": os.environ.get("PATH", ""), "PYTHONPATH": str(root),
           "SAUDADE_IGNORE_ENV_FILE": "1", "CHECKPOINT_DSN": "", "RAG_HYBRID_ENABLED": "0",
           "TRACE_DIR": str(tmp_path / "traces"),
           "SAUDADE_TASK_INPUT_TEST_DSN": os.environ["SAUDADE_TASK_INPUT_TEST_DSN"],
           "SAUDADE_CRASH_SCHEMA_OPTIONS": pool.kwargs["options"],
           "SAUDADE_CRASH_SIGNING_SECRET": secret}
    child = subprocess.run([sys.executable, str(root / "tests/_pg_lifecycle_worker.py"),
                            phase, str(marker)], cwd=tmp_path, env=env,
                           capture_output=True, text=True, timeout=45)
    assert child.returncode == 73, child.stderr
    store = NativeConversationStore(PostgresSaver(pool))
    saved = store.load(config())
    assert saved.unfinished and saved.run["id"] == "crash-task"
    model = Model(AIMessage(content="只撤销本操作，没有断言当前收藏状态。"))
    rebuilt = NativeRuntime(store, model_factory=lambda **_: model)
    with favorite_backend() as state:
        monkeypatch.setattr(confirm.settings, "jwt_secret", secret)
        if phase == "feedback":
            assert marker.read_text() == "revoked"
            old = confirm.verify(saved.pending_confirm["token"], 7, 42)
            domain = rebuilt.application.confirmation(old)
            name, args = domain.validate(old, config())
            row = writes.ledger_for(store.saver).lookup(writes.operation_key(old, name, args))
            assert row is not None and "confirmation_revoked" in str(row)
        result = list(rebuilt.stream_events({"resume_task": "crash-task"}, config()))[-1]
        assert not [request for request in state["requests"] if request.method != "GET"]
    final = NativeConversationStore(PostgresSaver(pool)).load(config())
    assert not final.unfinished and final.run is None
    assert len(model.prompts) == (0 if phase == "commit" else 1)
    if phase == "commit":
        assert [message.content for message in final.messages] == [
            "准备任务", "进程退出后保留的答复。"]
        assert result.reply == "进程退出后保留的答复。"
    else:
        assert not final.has_pending
        assert sum(isinstance(message, HumanMessage) and
                   message.content == "先不收藏，解释一下。" for message in final.messages) == 1
