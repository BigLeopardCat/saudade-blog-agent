"""Pending favorite changes survive chat; cancellation invalidates old grants."""

import asyncio
import json

import pytest
from _own_support import config
from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage
from langgraph.checkpoint.memory import InMemorySaver

from agent import confirm, idem
from agent.domains.favorites import preparation, transactions
from agent.runtime import pending, writes
from agent.runtime.confirmation import confirmed_runtime
from agent.runtime.native import NativeRuntime
from agent.runtime.native_store import NativeConversationStore
from agent.runtime.results import TurnResult
from tools import base


def test_pinned_executor_cannot_fall_back_when_persistence_is_disabled():
    with pytest.raises(RuntimeError, match="需要持久化"):
        confirmed_runtime(NativeRuntime(NativeConversationStore(None)), {"executor": "favorites"})


class Model:
    def __init__(self, name=None, args=None):
        self.prompts = []
        self.responses = ([AIMessage(content="", tool_calls=[{
            "name": ("load_pending_info_tools" if name == "cancel_pending_operation"
                     else "load_favorites_info_tools"), "args": {}, "id": "load"}]),
            AIMessage(content="", tool_calls=[{
                "name": name, "args": args or {}, "id": "pending"}])]
            if name else [AIMessage(content="你好，有什么想聊的？")])

    def bind_tools(self, tools, **kwargs):
        return self

    def invoke(self, prompt):
        self.prompts.append(list(prompt))
        return self.responses.pop(0)


@pytest.fixture
def waiting(site, monkeypatch, tmp_path):
    ledger = idem.Ledger(str(tmp_path / "pending.sqlite3"))
    monkeypatch.setattr(writes, "ledger_for", lambda saver: ledger)
    runtime = NativeRuntime(NativeConversationStore(InMemorySaver()))
    proposal = preparation.prepare_favorite_change.invoke(
        {"action": "add", "article_id": 19}, config=config())
    runtime.store.save_turn(config(), [HumanMessage(content="收藏文章19")], proposal.reply,
                            execution=proposal.execution, pending=proposal.execution)
    yield runtime, proposal, ledger
    ledger.close()


def run(runtime, model, text="取消刚才的操作"):
    runtime.model_factory = lambda **_: model
    return list(runtime.stream_events({"messages": [HumanMessage(content=text)]}, config()))


def test_cancellation_is_durable_before_reply_and_old_grant_cannot_write(
        waiting, site, monkeypatch):
    runtime, proposal, _ = waiting
    model = Model("cancel_pending_operation")
    runtime.model_factory = lambda **_: model
    events = list(runtime.stream_events(
        {"messages": [HumanMessage(content="先别收藏了")]}, config()))
    assert events[-1].execution["pending_event"]["event"] == "cancelled"
    assert runtime.get_state(config()).values["pending_action"] is None
    grant = confirm.verify(proposal.execution["pending_confirm"]["token"], 7, 42)
    result = transactions.execute(grant, config(), runtime.saver)
    assert result["receipts"] == [] and "撤销" in result["reply"]
    assert all(request.method == "GET" for request in site["requests"])
    token = proposal.execution["pending_confirm"]["token"]
    assert all(token not in str(message.content)
               for prompt in model.prompts for message in prompt)


def test_chat_preserves_pending_without_emitting_another_card(waiting, monkeypatch):
    runtime, proposal, _ = waiting
    events = run(runtime, Model(), "你好")
    expected = proposal.execution["pending_confirm"]
    assert runtime.get_state(config()).values["pending_confirm"] == expected
    assert not any(getattr(event, "execution", None) for event in events[:-1])


def test_replacement_revokes_old_card_and_stale_click_preserves_new_card(
        waiting, site, monkeypatch):
    runtime, proposal, _ = waiting
    original = base._get
    monkeypatch.setattr(base, "_get", lambda path, **kw: (
        {"noteId": 20, "noteTitle": "另一篇真实文章"} if path == "/notes/20"
        else original(path, **kw)))
    model = Model("prepare_favorite_change", {"action": "add", "article_id": 20})
    events = run(runtime, model, "改成收藏文章20")
    execution = next(event.execution for event in events
                     if getattr(event, "execution", None) and "pending_confirm" in event.execution)
    old = proposal.execution["pending_confirm"]
    new = execution["pending_confirm"]
    assert old["token"] != new["token"] and "另一篇真实文章" in new["q"]
    assert execution["pending_event"]["jti"] == confirm.token_jti(old["token"])
    stale = {"messages": [], "confirm_grant": confirm.verify(old["token"], 7, 42)}
    list(runtime.stream_events(stale, config()))
    assert runtime.get_state(config()).values["pending_confirm"] == new
    assert all(request.method == "GET" for request in site["requests"])


def test_replacement_to_satisfied_state_clears_old_proposal(waiting, monkeypatch, site):
    runtime, _, _ = waiting
    events = run(runtime, Model("prepare_favorite_change", {
        "action": "remove", "article_id": 19}), "改成取消收藏19")
    assert runtime.get_state(config()).values["pending_action"] is None
    execution = next(event.execution for event in events
                     if getattr(event, "execution", None) and "noop_note" in event.execution)
    assert execution["noop_note"] and execution["pending_event"]
    assert all(request.method == "GET" for request in site["requests"])


def test_failed_target_read_does_not_revoke_old_proposal(waiting):
    runtime, proposal, ledger = waiting
    active = pending.restore(runtime.get_state(config()).values, config())
    result = preparation.prepare_favorite_change.invoke(
        {"action": "add", "article_id": 999}, config=config())
    assert not isinstance(result, TurnResult)
    grant = active.grant
    name, args = transactions.validate(grant, config())
    state, _ = ledger.claim(writes.operation_key(grant, name, args), name, grant["jti"])
    assert state == "new"
    expected = proposal.execution["pending_confirm"]
    assert runtime.get_state(config()).values["pending_confirm"] == expected


@pytest.mark.parametrize("status", ["intent", "done"])
def test_cannot_cancel_a_started_or_completed_write(waiting, status):
    runtime, _, ledger = waiting
    active = pending.restore(runtime.get_state(config()).values, config())
    name, args = transactions.validate(active.grant, config())
    key = writes.operation_key(active.grant, name, args)
    ledger.claim(key, name, active.grant["jti"])
    if status == "done":
        ledger.finish(key, base.ToolResult("已收藏", "ok", {}))
    result = active.cancel_tool(runtime.saver).invoke({}, config=config())
    assert result.execution is None and "不能" in result.reply


def test_restore_rejects_cross_identity_and_mismatched_card(waiting):
    runtime, _, _ = waiting
    prior = runtime.get_state(config()).values
    assert pending.restore(prior, config(user_id=8)) is None
    prior["pending_action"] = {**prior["pending_action"], "jti": "f" * 32}
    assert pending.restore(prior, config()) is None


@pytest.mark.parametrize("fault", ["ledger", "checkpoint"])
def test_storage_failure_emits_no_cancellation_and_old_card_still_cannot_write(
        waiting, monkeypatch, site, fault):
    runtime, proposal, ledger = waiting

    def failed(*args, **kwargs):
        raise RuntimeError("storage unavailable")

    if fault == "ledger":
        monkeypatch.setattr(ledger, "finish", failed)
    else:
        monkeypatch.setattr(runtime.store, "update_state", failed)
    runtime.model_factory = lambda **_: Model("cancel_pending_operation")
    stream = runtime.stream_events({"messages": [HumanMessage(content="取消待办")]}, config())
    with pytest.raises(RuntimeError, match="storage unavailable"):
        list(stream)
    assert runtime.get_state(config()).values["pending_confirm"] == proposal.execution[
        "pending_confirm"]
    grant = confirm.verify(proposal.execution["pending_confirm"]["token"], 7, 42)
    result = transactions.execute(grant, config(), runtime.saver)
    assert result["blocked"] and result["receipts"] == []
    assert all(request.method == "GET" for request in site["requests"])


def test_stream_cancels_host_pending_and_routes_revoked_card_after_runtime_switch(
        waiting, monkeypatch, site):
    import server
    from agent.runtime.native import NativeRuntime
    from agent.runtime.native_store import NativeConversationStore

    runtime, proposal, ledger = waiting

    async def collect(runtime, grant=None):
        monkeypatch.setattr(server, "_agent", runtime)
        queue = asyncio.Queue()
        producer = asyncio.create_task(asyncio.to_thread(
            server._run_agent_stream_to_queue, [HumanMessage(content="取消待办")],
            "conv_42", queue, asyncio.get_running_loop(), user_id=7,
            principal=config()["configurable"]["principal"], conversation_id=42,
            confirm_grant=grant))
        frames = []
        while (frame := await asyncio.wait_for(queue.get(), 5)) is not None:
            frames.append(frame)
        await producer
        return frames

    from contextlib import closing

    class Store:
        def claim(self, *args, **kwargs):
            with closing(idem.Ledger(ledger.path)) as connection:
                return connection.claim(*args, **kwargs)

        def finish(self, *args):
            with closing(idem.Ledger(ledger.path)) as connection:
                return connection.finish(*args)

    monkeypatch.setattr(writes, "ledger_for", lambda saver: Store())
    frames = asyncio.run(collect(NativeRuntime(
        NativeConversationStore(runtime.saver),
        model_factory=lambda **_: Model("cancel_pending_operation"))))
    controls = [json.loads(frame.removeprefix("__PENDING__:")) for frame in frames
                if isinstance(frame, str) and frame.startswith("__PENDING__:")]
    assert controls == [{"event": "cancelled", "task_id": proposal.execution[
        "pending_action"]["task_id"], "jti": proposal.execution["pending_action"]["jti"]}]
    grant = confirm.verify(proposal.execution["pending_confirm"]["token"], 7, 42)
    assert grant["executor"] == "favorites"
    # The raw runtime's stream is bypassed; the signed executor takes over.
    replay = asyncio.run(collect(NativeRuntime(
        NativeConversationStore(runtime.saver)), grant))
    assert any("撤销" in str(frame.content) for frame in replay
               if isinstance(frame, AIMessageChunk))
    assert not any(str(frame).startswith("__EXEC__:") for frame in replay)
    assert all(request.method == "GET" for request in site["requests"])


def test_button_cancel_is_idempotent_and_rejects_later_approval(waiting, site):
    from agent.runtime.confirmation import cancel

    runtime, proposal, _ = waiting
    token = proposal.execution["pending_confirm"]["token"]
    grant = confirm.verify(token, 7, 42)
    result = cancel(runtime, grant, config())
    assert result["cancelled"] is True
    assert cancel(runtime, grant, config()) == result
    assert runtime.get_state(config()).values["pending_action"] is None
    replay = transactions.execute(grant, config(), runtime.saver)
    assert replay["receipts"] == [] and "撤销" in replay["reply"]
    assert all(request.method == "GET" for request in site["requests"])
    assert [op["value"] for op in proposal.execution["pending_confirm"]["opts"]] == ["yes", "no"]


@pytest.mark.parametrize("status", ["intent", "done"])
def test_button_cancel_cannot_claim_a_started_write_was_cancelled(waiting, status):
    from agent.runtime.confirmation import cancel

    runtime, proposal, ledger = waiting
    grant = confirm.verify(proposal.execution["pending_confirm"]["token"], 7, 42)
    name, args = transactions.validate(grant, config())
    key = writes.operation_key(grant, name, args)
    ledger.claim(key, name, grant["jti"])
    if status == "done":
        ledger.finish(key, base.ToolResult("已收藏", "ok", {}))
    assert cancel(runtime, grant, config())["cancelled"] is False
    assert runtime.get_state(config()).values["pending_confirm"] is not None


def test_stale_button_cancel_preserves_replacement(waiting, monkeypatch, site):
    from agent.runtime.confirmation import cancel

    runtime, proposal, _ = waiting
    grant = confirm.verify(proposal.execution["pending_confirm"]["token"], 7, 42)
    original = base._get
    monkeypatch.setattr(base, "_get", lambda path, **kw: (
        {"noteId": 20, "noteTitle": "另一篇真实文章"} if path == "/notes/20"
        else original(path, **kw)))
    list(run(runtime, Model("prepare_favorite_change", {
        "action": "add", "article_id": 20}), "换成20"))
    expected = runtime.get_state(config()).values["pending_confirm"]
    assert cancel(runtime, grant, config())["cancelled"] is True
    assert runtime.get_state(config()).values["pending_confirm"] == expected


def test_cancellation_api_checks_signed_identity_and_never_calls_a_model(
        waiting, monkeypatch, service_workers):
    from contextlib import closing

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    import server
    from agent.approval_api import router

    runtime, proposal, ledger = waiting
    token = proposal.execution["pending_confirm"]["token"]
    monkeypatch.setattr(server, "_agent", runtime)
    monkeypatch.setattr(server.settings, "agent_require_assertion", True)
    from agent.api import authentication
    monkeypatch.setattr(authentication, "verify_assertion_claims", lambda tok: (
        {"uid": 7, "role": "user"} if tok == "fixture" else None))

    class Store:
        def claim(self, *args, **kwargs):
            with closing(idem.Ledger(ledger.path)) as connection:
                return connection.claim(*args, **kwargs)

        def finish(self, *args):
            with closing(idem.Ledger(ledger.path)) as connection:
                return connection.finish(*args)

    monkeypatch.setattr(writes, "ledger_for", lambda saver: Store())
    app = FastAPI()
    app.state.workers = service_workers
    app.include_router(router(lambda: server._agent, server._resolve_principal))
    with TestClient(app) as client:
        body = {"user_id": 7, "conversation_id": 42, "token": token}
        assert client.post("/confirmation/cancel", json=body).status_code == 401
        headers = {"X-Agent-Assertion": "fixture"}
        for change in ({"conversation_id": 43}, {"token": token + "x"}):
            assert client.post("/confirmation/cancel", json={**body, **change},
                               headers=headers).status_code == 403
        for _ in range(2):
            response = client.post("/confirmation/cancel", json=body, headers=headers)
            assert response.status_code == 200 and response.json()["cancelled"] is True


def test_unknown_executor_cannot_enter_legacy_planner():
    with pytest.raises(ValueError, match="执行入口"):
        confirmed_runtime(NativeRuntime(NativeConversationStore(None)), {"executor": "unknown"})
