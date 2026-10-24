"""Real tool and HTTP boundaries for signed personal todo operations."""

import json
import secrets

import httpx
import pytest
from _own_support import config
from pydantic import ValidationError

from agent import confirm, idem
from agent.domains.todos import preparation, transactions
from agent.principal import SOURCE_ASSERTION, Principal
from agent.runtime import writes
from agent.runtime.pending import PendingOperation
from agent.runtime.results import TurnResult
from tools import base

VERSION = "2026-10-23 10:00:00.000001"


def admin_config(**values):
    return config(principal=Principal(7, "admin", SOURCE_ASSERTION), **values)


@pytest.fixture
def todos(monkeypatch):
    state = {
        "rows": [
            {"id": 11, "version": VERSION, "text": "更新简历", "done": False, "date": "2026-10-24"}
        ],
        "requests": [],
        "writes": 0,
        "ignore": False,
        "after_unavailable": False,
        "write_error": False,
    }

    def handle(request):
        state["requests"].append(request)
        assert request.headers.get("Authorization", "").startswith("Bearer ")
        if request.method == "GET":
            if state["after_unavailable"] and state["writes"]:
                return httpx.Response(503)
            return httpx.Response(200, json={"code": 200, "data": state["rows"]})
        payload = json.loads(request.content)
        state["writes"] += 1
        if state["write_error"]:
            raise httpx.ReadTimeout("request sent; response lost")
        if request.url.path.endswith("/item"):
            row = {
                "id": 20,
                "text": payload["text"],
                "done": False,
                "date": payload.get("date"),
                "version": VERSION,
            }
            if not state["ignore"]:
                state["rows"].append(row)
        else:
            row = next(row for row in state["rows"] if row["id"] == payload["id"])
            if payload["version"] != row["version"] or payload["text"] != row["text"]:
                return httpx.Response(200, json={"code": 500, "message": "待办已变化", "data": {}})
            row = dict(row)
            if request.url.path.endswith("/done"):
                row["done"] = payload["done"]
            else:
                row["date"] = payload["date"]
            row["version"] = "2026-10-23 10:00:01.000001"
            if not state["ignore"]:
                state["rows"] = [row if old["id"] == row["id"] else old for old in state["rows"]]
        return httpx.Response(200, json={"code": 200, "data": row})

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        monkeypatch.setattr(base, "_client", client)
        monkeypatch.setattr(confirm.settings, "jwt_secret", secrets.token_urlsafe(32))
        yield state


def prepare(action, **kwargs):
    return preparation.prepare_dashboard_todo.invoke(
        {"action": action, **kwargs}, config=admin_config()
    )


def grant(result):
    return confirm.verify(result.execution["pending_confirm"]["token"], 7, 42)


@pytest.mark.parametrize(
    "action,args,workflow,name",
    [
        (
            "create",
            {"text": "准备面试", "date": "2026-10-26"},
            "dashboard_todo_add",
            "create_dashboard_todo",
        ),
        ("complete", {"item_id": 11}, "dashboard_todo_done", "complete_dashboard_todo"),
        (
            "reschedule",
            {"item_id": 11, "date": None},
            "dashboard_todo_reschedule",
            "reschedule_dashboard_todo",
        ),
    ],
)
def test_preparation_zero_writes_binds_exact_arguments(todos, action, args, workflow, name):
    result = prepare(action, **args)
    assert isinstance(result, TurnResult)
    signed = grant(result)
    assert signed["executor"] == "todos" and signed["workflow"] == workflow
    signed_args = signed["specs"][0]["args"]
    assert signed["specs"][0]["tool"] == name
    assert signed_args["text"] == args.get("text", "更新简历")
    if action != "create":
        assert signed_args["item_id"] == 11 and signed_args["version"] == VERSION
    assert todos["writes"] == 0
    assert all(request.method == "GET" for request in todos["requests"])
    assert result.execution["pending_confirm"]["token"] not in result.reply
    assert not result.execution["receipts"]


@pytest.mark.parametrize(
    "action,args",
    [
        ("create", {"text": "准备面试"}),
        ("complete", {"item_id": 11}),
        ("reschedule", {"item_id": 11, "date": "2026-10-25"}),
    ],
)
def test_confirmed_operations_verify_actual_state_and_replay_once(todos, tmp_path, action, args):
    signed = grant(prepare(action, **args))
    ledger = idem.Ledger(str(tmp_path / "writes.sqlite3"))
    first = transactions.execute(signed, admin_config(), None, ledger=ledger)
    second = transactions.execute(signed, admin_config(), None, ledger=ledger)
    assert todos["writes"] == 1
    assert len(first["receipts"]) == len(second["receipts"]) == 1
    assert first["receipts"][0]["args"] == signed["specs"][0]["args"]
    assert not first["blocked"] and first["reply"] == second["reply"]
    if action == "create":
        assert todos["rows"][-1]["text"] == "准备面试"
    elif action == "complete":
        assert todos["rows"][0]["done"] is True
    else:
        assert todos["rows"][0]["date"] == "2026-10-25"
        assert "2026-10-25" in first["reply"]


@pytest.mark.parametrize(
    "change",
    [
        {"version": "2026-10-23 10:00:02"},
        {"text": "新待办"},
        {"id": 13},
    ],
)
def test_replaced_or_edited_target_is_zero_write(todos, tmp_path, change):
    signed = grant(prepare("complete", item_id=11))
    todos["rows"][0].update(change)
    result = transactions.execute(
        signed, admin_config(), None, ledger=idem.Ledger(str(tmp_path / "stale.sqlite3"))
    )
    assert todos["writes"] == 0 and not result["receipts"]
    assert "已变化" in result["reply"]


@pytest.mark.parametrize("mode", ["ignore", "after_unavailable", "write_error"])
def test_unknown_outcome_never_claims_completion_or_repeats(todos, tmp_path, mode):
    signed = grant(prepare("create", text="准备面试"))
    todos[mode] = True
    ledger = idem.Ledger(str(tmp_path / "uncertain.sqlite3"))
    first = transactions.execute(signed, admin_config(), None, ledger=ledger)
    second = transactions.execute(signed, admin_config(), None, ledger=ledger)
    assert todos["writes"] == 1
    assert not first["receipts"] and not second["receipts"]
    assert first["blocked"] and second["blocked"]


@pytest.mark.parametrize(
    "action,args",
    [
        ("create", {"text": "x", "date": "明天"}),
        ("create", {"text": "x", "date": "2026-02-30"}),
        ("complete", {"item_id": 99}),
        ("complete", {"item_id": 11, "text": "更新简历"}),
        ("complete", {"item_id": 11, "date": "2026-10-25"}),
        ("reschedule", {"item_id": 11, "date": "2026-1-1"}),
        ("reschedule", {"item_id": 11}),
        ("reschedule", {"item_id": 11, "date": ""}),
    ],
)
def test_invalid_argument_combinations_never_prepare_or_write(todos, action, args):
    result = prepare(action, **args)
    assert isinstance(result, base.ToolResult) and result.kind == "unavailable"
    assert not todos["writes"]


@pytest.mark.parametrize(
    "role,source", [("user", SOURCE_ASSERTION), ("admin", "body"), ("superadmin", "body")]
)
def test_non_console_or_unverified_actor_never_reads_or_prepares(todos, role, source):
    result = preparation.prepare_dashboard_todo.invoke(
        {"action": "create", "text": "x"}, config=config(principal=Principal(7, role, source))
    )
    assert result.kind == "unavailable" and not todos["requests"]


@pytest.mark.parametrize(
    "args",
    [
        {"action": "complete", "item_id": True},
        {"action": "create", "text": 3},
        {"action": "reschedule", "item_id": "11"},
    ],
)
def test_tool_schema_rejects_coercion(todos, args):
    with pytest.raises(ValidationError):
        preparation.prepare_dashboard_todo.invoke(args, config=admin_config())
    assert not todos["requests"]


@pytest.mark.parametrize(
    "change", [{"uid": 8}, {"conv": 43}, {"exp": 1}, {"executor": "favorites"}, {"specs": []}]
)
def test_invalid_binding_never_claims_or_calls(todos, change):
    signed = {**grant(prepare("complete", item_id=11)), **change}
    before = len(todos["requests"])

    class NoLedger:
        def claim(self, *args, **kwargs):
            pytest.fail("invalid binding reached ledger")

    result = transactions.execute(signed, admin_config(), None, ledger=NoLedger())
    assert not result["receipts"] and result["blocked"]
    assert len(todos["requests"]) == before


def test_duplicate_text_is_safe_with_stable_id(todos, tmp_path):
    todos["rows"].append({**todos["rows"][0], "id": 12})
    result = transactions.execute(
        grant(prepare("complete", item_id=11)),
        admin_config(),
        None,
        ledger=idem.Ledger(str(tmp_path / "duplicates.sqlite3")),
    )
    assert result["receipts"]
    assert todos["rows"][0]["done"] is True and todos["rows"][1]["done"] is False


def test_cancel_and_cross_domain_replacement_use_shared_revocation(todos, monkeypatch, tmp_path):
    from agent.domains import registry
    from agent.domains.notifications import transactions as notifications

    monkeypatch.setitem(registry._DOMAINS, "todos", transactions)
    ledger = idem.Ledger(str(tmp_path / "revoke.sqlite3"))
    monkeypatch.setattr(writes, "ledger_for", lambda saver: ledger)
    initial = prepare("complete", item_id=11)
    signed = grant(initial)
    pending = PendingOperation(signed, initial.execution["pending_action"])
    replacement = TurnResult(
        "通知确认",
        {
            "pending_confirm": {"token": secrets.token_urlsafe(32)},
            "pending_action": {"task_id": "new"},
        },
    )
    transitioned = pending.transition(replacement, admin_config(), None)
    assert transitioned.execution["pending_event"]["event"] == "cancelled"
    assert transitioned.execution["pending_confirm"] == replacement.execution["pending_confirm"]
    old = transactions.execute(signed, admin_config(), None, ledger=ledger)
    assert not old["receipts"] and todos["writes"] == 0
    assert "撤销" in old["reply"]
    assert notifications.handles({"executor": "notifications", "workflow": "notice_read"})


def test_cancelled_card_remains_revoked(todos, monkeypatch, tmp_path):
    from agent.domains import registry

    monkeypatch.setitem(registry._DOMAINS, "todos", transactions)
    ledger = idem.Ledger(str(tmp_path / "cancel.sqlite3"))
    monkeypatch.setattr(writes, "ledger_for", lambda saver: ledger)
    result = prepare("create", text="准备面试")
    signed = grant(result)
    pending = PendingOperation(signed, result.execution["pending_action"])
    cancelled = pending.cancel_tool(None).invoke({}, config=admin_config())
    assert cancelled.execution["pending_action"] is None
    assert cancelled.execution["pending_event"]["jti"] == signed["jti"]
    replay = transactions.execute(signed, admin_config(), None, ledger=ledger)
    assert not replay["receipts"] and todos["writes"] == 0


@pytest.mark.parametrize(
    "action,args",
    [
        ("create", {"text": "准备面试", "date": "2026-10-26"}),
        ("complete", {"item_id": 11}),
        ("reschedule", {"item_id": 11, "date": None}),
    ],
)
def test_hybrid_prepares_and_executes_without_legacy_or_followup_model(
    todos, monkeypatch, tmp_path, action, args
):
    from langchain_core.messages import AIMessage, HumanMessage
    from langgraph.checkpoint.memory import InMemorySaver

    from agent.runtime.native import NativeRuntime
    from agent.runtime.native_store import NativeConversationStore

    class Model:
        def __init__(self):
            self.calls = []
            self.responses = [
                AIMessage(
                    content="",
                    tool_calls=[
                        {"name": "load_admin_work_queue_info_tools", "args": {}, "id": "load"}
                    ],
                ),
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "prepare_dashboard_todo",
                            "args": {"action": action, **args},
                            "id": "prepare",
                        }
                    ],
                ),
            ]

        def bind_tools(self, tools, **kwargs):
            return self

        def invoke(self, prompt):
            self.calls.append(list(prompt))
            return self.responses.pop(0)

    model = Model()
    ledger = idem.Ledger(str(tmp_path / "hybrid.sqlite3"))
    monkeypatch.setattr(writes, "ledger_for", lambda saver: ledger)
    runtime = NativeRuntime(NativeConversationStore(InMemorySaver()),
                            model_factory=lambda **_: model)
    prepared = list(runtime.stream_events(
        {"messages": [HumanMessage(content="按要求改个人待办")]}, admin_config()))
    execution = prepared[-1].execution
    assert todos["writes"] == 0 and len(model.calls) == 2
    token = execution["pending_confirm"]["token"]
    assert token not in repr(model.calls)
    signed = confirm.verify(token, 7, 42)
    assert runtime.get_state(admin_config()).values["pending_confirm"]["token"] == token
    for _ in range(2):
        completed = list(runtime.stream_events(
            {"messages": [HumanMessage(content="确认")], "confirm_grant": signed},
            admin_config()))
        result = completed[-1].execution
        assert result["receipts"][0]["tool"] == signed["specs"][0]["tool"]
    assert todos["writes"] == 1 and len(model.calls) == 2
    assert runtime.get_state(admin_config()).values["pending_action"] is None


@pytest.mark.parametrize(
    "action,args",
    [
        ("complete", {"item_id": 11}),
        ("reschedule", {"item_id": 11, "date": "2026-10-24"}),
    ],
)
def test_satisfied_state_needs_no_card_or_post(todos, action, args):
    if action == "complete":
        todos["rows"][0]["done"] = True
    result = prepare(action, **args)
    assert result.execution["noop_note"] == result.reply
    assert "pending_confirm" not in result.execution and todos["writes"] == 0


@pytest.mark.parametrize(
    "field,value",
    [("id", None), ("version", None), ("done", 1), ("date", "tomorrow"), ("text", "")],
)
def test_malformed_snapshot_is_unknown_not_empty(todos, field, value):
    todos["rows"][0][field] = value
    result = prepare("create", text="准备面试")
    assert result.kind == "unavailable" and todos["writes"] == 0
