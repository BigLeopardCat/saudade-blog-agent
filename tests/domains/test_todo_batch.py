"""A signed selectable todo group executes only its selected subset, once."""

import json
import secrets
from contextlib import closing

import httpx
import pytest
from _framework_ledger import Ledger
from _own_support import config

from agent import confirm
from agent.domains.todos import batch, transactions
from agent.principal import SOURCE_ASSERTION, Principal
from tools import base

VERSION = "2026-11-03 10:00:00.000001"


@pytest.fixture
def selection(monkeypatch, tmp_path):
    rows = [
        {"id": value, "text": f"待办{value}", "version": VERSION, "done": True, "date": None}
        for value in [11, 12, 13]
    ]
    state = {"rows": rows, "writes": 0, "requests": [], "bad_receipt": False}

    def handle(request):
        state["requests"].append(request)
        payload = json.loads(request.content)
        if request.url.path.endswith("/batch/prepare"):
            assert request.method == "POST"
            data = [row for row in state["rows"] if row["id"] in payload["ids"]]
            return httpx.Response(200, json={"code": 200, "data": data})
        assert request.url.path.endswith("/batch") and request.method == "DELETE"
        for item in payload["items"]:
            if not any(
                all(row[key] == value for key, value in item.items()) for row in state["rows"]
            ):
                return httpx.Response(200, json={"code": 409, "message": "changed", "data": []})
        state["writes"] += 1
        ids = {item["id"] for item in payload["items"]}
        state["rows"] = [row for row in state["rows"] if row["id"] not in ids]
        data = [
            {"id": item["id"], "version": item["version"], "deleted": True}
            for item in payload["items"]
        ]
        if state["bad_receipt"]:
            data.pop()
        return httpx.Response(200, json={"code": 200, "data": data})

    cfg = config(principal=Principal(7, "admin", SOURCE_ASSERTION))
    monkeypatch.setattr(confirm.settings, "jwt_secret", secrets.token_urlsafe(32))
    with (
        httpx.Client(transport=httpx.MockTransport(handle)) as client,
        closing(Ledger(str(tmp_path / "execution.sqlite3"))) as ledger,
    ):
        monkeypatch.setattr(base, "_client", client)
        from agent.runtime import writes

        monkeypatch.setattr(writes, "ledger_for", lambda _: ledger)
        yield state, cfg, ledger


def prepare(cfg):
    result = batch.prepare_dashboard_todo_deletions.invoke({"item_ids": [13, 11, 12]}, config=cfg)
    return result, confirm.verify(result.execution["pending_confirm"]["token"], 7, 42)


def test_preparation_is_read_only_and_approving_two_preserves_the_unselected_todo(selection):
    state, cfg, ledger = selection
    result, grant = prepare(cfg)
    assert len(grant["specs"]) == 3 and state["writes"] == 0
    choices = [
        option
        for option in result.execution["pending_confirm"]["opts"]
        if option["kind"] == "choice"
    ]
    assert [option["value"] for option in choices] == ["pick:0", "pick:1", "pick:2"]
    selected, error = confirm.narrow(grant, "pick:0,2")
    assert not error
    first = transactions.execute(selected, cfg, None, ledger=ledger)
    assert len(first["receipts"]) == 1 and state["writes"] == 1
    assert [row["id"] for row in state["rows"]] == [12]
    assert [item["id"] for item in first["receipts"][0]["args"]["items"]] == [11, 13]
    transactions.execute(selected, cfg, None, ledger=ledger)
    assert state["writes"] == 1
    another, _ = confirm.narrow(grant, "pick:1")
    replay = {"configurable": {**cfg["configurable"], "confirmation_replay_only": True}}
    blocked = transactions.execute(another, replay, None, ledger=ledger)
    assert not blocked["receipts"] and state["writes"] == 1
    assert [row["id"] for row in state["rows"]] == [12]


def test_changed_or_incomplete_target_never_prepares_or_executes(selection):
    state, cfg, ledger = selection
    _, grant = prepare(cfg)
    state["rows"][1]["version"] = "2026-11-03 10:01:00.000001"
    result = transactions.execute(grant, cfg, None, ledger=ledger)
    assert not result["receipts"] and state["writes"] == 0
    assert len(state["rows"]) == 3
    incomplete = batch.prepare_dashboard_todo_deletions.invoke({"item_ids": [11, 99]}, config=cfg)
    assert incomplete.kind == "unavailable"


def test_partial_receipt_never_reports_completion_or_replays_write(selection):
    state, cfg, ledger = selection
    _, grant = prepare(cfg)
    state["bad_receipt"] = True
    result = transactions.execute(grant, cfg, None, ledger=ledger)
    assert not result["receipts"] and result["operation_outcome"]
    transactions.execute(grant, cfg, None, ledger=ledger)
    assert state["writes"] == 1


def test_wrong_role_and_duplicate_ids_never_read_or_write(selection):
    state, cfg, _ = selection
    user = config(principal=Principal(7, "user", SOURCE_ASSERTION))
    denied = batch.prepare_dashboard_todo_deletions.invoke({"item_ids": [11, 12]}, config=user)
    assert denied.kind == "unavailable" and not state["requests"]
    duplicate = batch.prepare_dashboard_todo_deletions.invoke({"item_ids": [11, 11]}, config=cfg)
    assert duplicate.kind == "invalid_args" and not state["requests"]


def test_framework_continuation_records_unselected_work_as_retained(selection):
    from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
    from langgraph.checkpoint.memory import InMemorySaver
    from test_confirmation_continuation import Model, call

    from agent.products.blog.application import BlogApplication
    from agent.runtime.checkpoint_conversations import CheckpointConversationStore
    from agent.runtime.service import AgentRuntime

    state, cfg, _ = selection
    first = Model(
        AIMessage(content="", tool_calls=[call("load_admin_work_queue_info_tools", "load")]),
        AIMessage(
            content="",
            tool_calls=[call("prepare_dashboard_todo_deletions", "prepare", item_ids=[11, 12, 13])],
        ),
    )
    selected = AgentRuntime(
        CheckpointConversationStore(InMemorySaver()),
        application=BlogApplication(),
        model_factory=lambda **_: first,
    )
    initial = list(
        selected.stream_events({"messages": [HumanMessage(content="删除三项待办")]}, cfg)
    )[-1]
    grant = confirm.verify(initial.execution["pending_confirm"]["token"], 7, 42)
    narrowed, error = confirm.narrow(grant, "pick:0,2")
    assert not error
    final = Model(AIMessage(content="已删除选中的两项，未选项已保留。"))
    selected.model_factory = lambda **_: final
    result = list(selected.stream_events({"confirm_grant": narrowed}, cfg))[-1]
    assert result.reply == "已删除选中的两项，未选项已保留。"
    assert result.execution["approval_selection"]["unselected"] == "retained_not_queued"
    facts = [
        json.loads(message.content)
        for message in final.prompts[-1]
        if isinstance(message, ToolMessage) and message.content.startswith("{")
    ]
    assert any(fact.get("selection", {}).get("selected_units") == 2 for fact in facts)
    other, _ = confirm.narrow(grant, "pick:1")
    selected.model_factory = lambda **_: pytest.fail("a settled selection must not call the model")
    list(selected.stream_events({"confirm_grant": other}, cfg))
    assert state["writes"] == 1 and [row["id"] for row in state["rows"]] == [12]
