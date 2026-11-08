"""Conversation ownership precedes framework state access and execution authority."""

import asyncio
import secrets
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from threading import Barrier
from types import SimpleNamespace

import httpx
import pytest
import test_confirmation_continuation as continuation
from _conversation_seed import seed_turn
from _memory_support import ACTOR, Capture, config, runtime
from _service import cleanup_headers
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.memory import InMemorySaver

from agent import checkpoint
from agent.main import create_app
from agent.principal import SOURCE_ASSERTION, Principal
from agent.products.blog.confirmation import cancel, current_card
from agent.runtime import approval, memory
from agent.runtime.checkpoint_conversations import CheckpointConversationStore
from agent.runtime.conversation_store import ConversationAccessDenied
from agent.runtime.task_management import cancel_paused
from tools.result import ok

FOREIGN = Principal(8, "user", SOURCE_ASSERTION)
backend = continuation.backend


@pytest.mark.parametrize("operation", [
    "load", "state", "history", "resume_probe", "update", "clear", "prune",
    "turn", "resume", "confirm", "card", "delete", "discard",
])
def test_foreign_actor_cannot_read_modify_resume_confirm_or_delete(operation):
    saver, model = InMemorySaver(), Capture()
    selected = runtime(saver, model)
    selected.update_state(config(), {"messages": [HumanMessage(content="PRIVATE_CANARY")],
        "pending_action": {"jti": "waiting"},
        "pending_confirm": {"token": secrets.token_urlsafe(24)}})
    before = deepcopy((saver.storage, saver.writes))
    foreign = config(FOREIGN)
    operations = {
        "load": lambda: selected.store.load(foreign),
        "state": lambda: selected.get_state(foreign, subgraphs=True),
        "history": lambda: selected.get_state({**foreign, "configurable": {
            **foreign["configurable"], "checkpoint_id": saver.get_tuple(config()).config[
                "configurable"]["checkpoint_id"]}}),
        "resume_probe": lambda: selected.store.is_resuming(foreign),
        "update": lambda: selected.update_state(
            foreign, {"messages": [HumanMessage(content="bad")]}),
        "clear": lambda: selected.store.clear_pending(foreign, "waiting"),
        "prune": lambda: selected.store.prune(foreign),
        "turn": lambda: list(selected.stream_events(
            {"messages": [HumanMessage(content="new")]}, foreign)),
        "resume": lambda: list(selected.stream_events({"resume_task": "paused"}, foreign)),
        "confirm": lambda: list(selected.stream_events(
            {"confirm_grant": {"jti": "waiting"}}, foreign)),
        "card": lambda: current_card(selected, FOREIGN, 42, None),
        "delete": lambda: checkpoint.delete_thread(saver, 42, principal=FOREIGN),
        "discard": lambda: checkpoint.drop_last_user_turn(selected, 42, "old", principal=FOREIGN),
    }
    with pytest.raises(ConversationAccessDenied, match="^conversation_not_found$"):
        operations[operation]()
    assert (saver.storage, saver.writes) == before
    assert not model.prompts
    assert not getattr(saver, "_deleted_threads", set())
    assert not getattr(saver, "_deleted_turns", set())


@pytest.mark.parametrize("principal,user_id", [
    (Principal(7, "user"), 7), (ACTOR, 8), (Principal(True, "user", SOURCE_ASSERTION), True),
])
def test_unverified_or_inconsistent_identity_cannot_claim_an_empty_thread(principal, user_id):
    saver = InMemorySaver()
    store = CheckpointConversationStore(saver)
    cfg = config(principal)
    cfg["configurable"]["user_id"] = user_id
    with pytest.raises(ConversationAccessDenied):
        store.update_state(cfg, {"messages": [HumanMessage(content="claim")]})
    assert saver.get_tuple(config()) is None


def test_caller_metadata_cannot_change_persisted_ownership():
    saver = InMemorySaver()
    store = CheckpointConversationStore(saver)
    spoofed = {**config(), "metadata": {"user_id": 8, "conversation_id": 99}}
    store.update_state(spoofed, {"messages": [HumanMessage(content="owner")]})
    assert saver.get_tuple(config()).metadata["user_id"] == 7
    with pytest.raises(ConversationAccessDenied):
        store.load({**config(FOREIGN), "metadata": {"user_id": 7, "conversation_id": 42}})


def test_opaque_framework_thread_ids_still_require_matching_owner_and_coordinates():
    saver = InMemorySaver()
    store = CheckpointConversationStore(saver)
    cfg = config()
    cfg["configurable"]["thread_id"] = "70dfca5e-3f6c-4933-83eb-7c6a1d7ee124"
    store.update_state(cfg, {"messages": [HumanMessage(content="owned")]})
    assert store.load(cfg).messages[0].content == "owned"
    before = deepcopy(saver.storage)
    for overrides in ({"conversation_id": 43}, {"user_id": 8, "principal": FOREIGN}):
        with pytest.raises(ConversationAccessDenied):
            store.load({"configurable": {**cfg["configurable"], **overrides}})
    assert saver.storage == before


def test_unbound_checkpoint_is_rejected_without_inference_from_message_text():
    saver = InMemorySaver()
    raw = {"configurable": {"thread_id": "conv_42"}}
    approval.graph(saver).update_state(raw, {"messages": [HumanMessage(
        content="I belong to user 7", additional_kwargs={"memory_actor": {"uid": 7}})]},
        as_node="commit")
    before = deepcopy(saver.storage)
    with pytest.raises(ConversationAccessDenied):
        CheckpointConversationStore(saver).load(config())
    assert saver.storage == before


def test_first_writers_racing_for_one_thread_cannot_replace_its_owner():
    saver, barrier = InMemorySaver(), Barrier(2)

    def claim(principal):
        barrier.wait(timeout=3)
        try:
            CheckpointConversationStore(saver).update_state(config(principal), {
                "messages": [HumanMessage(content=f"owner:{principal.uid}")]})
            return principal.uid
        except ConversationAccessDenied:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(claim, [ACTOR, FOREIGN]))
    winner, = [uid for uid in outcomes if uid is not None]
    snapshot = CheckpointConversationStore(saver).get_state(
        config(Principal(winner, "user", SOURCE_ASSERTION)))
    assert snapshot.metadata["user_id"] == winner
    assert [message.content for message in snapshot.values["messages"]] == [f"owner:{winner}"]


def test_role_change_keeps_owner_but_filters_privileged_history_and_allows_new_turn():
    saver, model = InMemorySaver(), Capture()
    selected = runtime(saver, model)
    admin = config(Principal(7, "admin", SOURCE_ASSERTION))
    call = {"name": "list_admin_notes", "args": {}, "id": "private"}
    seed_turn(selected.store, admin, [HumanMessage(content="inspect private data")],
              "PRIVATE_CANARY", observations=memory.observation(call, ok("PRIVATE_CANARY")))
    list(selected.stream_events({"messages": [HumanMessage(content="hello")]}, config()))
    assert model.prompts and "PRIVATE_CANARY" not in str(model.prompts)
    snapshot = selected.get_state(config())
    assert snapshot.metadata["user_id"] == 7
    assert any(message.content == "hello" for message in snapshot.values["messages"])
    assert checkpoint.delete_thread(saver, 42, principal=ACTOR)


def test_foreign_resume_preserves_native_interrupt_and_owner_can_continue():
    first = continuation.Model(AIMessage(content="", tool_calls=[
        continuation.call("pause_task", "hold", instruction="Explain checkpoints")]))
    selected = continuation.runtime(first)
    cfg = config()
    cfg["configurable"]["task_inputs"] = True
    result = list(selected.stream_events({"task_id": "held-task", "messages": [
        HumanMessage(content="hold this task")]}, cfg))[-1]
    assert result.execution["paused_task"]
    assert selected.get_state(cfg, subgraphs=True).interrupts
    before = deepcopy(selected.saver.storage)
    with pytest.raises(ConversationAccessDenied):
        list(selected.stream_events({"resume_task": "held-task"}, config(FOREIGN)))
    with pytest.raises(ConversationAccessDenied):
        cancel_paused(selected, FOREIGN, 42, "held-task")
    assert selected.saver.storage == before
    changed = config(Principal(7, "admin", SOURCE_ASSERTION))
    refused = list(selected.stream_events({"resume_task": "held-task"}, changed))[-1]
    assert refused.execution["blocked"] and len(first.prompts) == 1
    resumed = continuation.Model(AIMessage(content="Resumed from checkpoint."))
    rebuilt = continuation.runtime(resumed, selected.saver)
    assert list(rebuilt.stream_events({"resume_task": "held-task"}, cfg))[-1].reply
    assert len(resumed.prompts) == 1 and not rebuilt.store.load(cfg).unfinished


def test_foreign_confirmation_and_revocation_leave_owner_card_executable(backend):
    state, _ = backend
    selected = continuation.runtime(continuation.Model())
    _, grant, _ = continuation.prepare(selected, continuation.FAVORITE)
    before = deepcopy(selected.saver.storage)
    for action in (
        lambda: list(selected.stream_events({"confirm_grant": grant}, config(FOREIGN))),
        lambda: cancel(selected, grant, config(FOREIGN)),
    ):
        with pytest.raises(ConversationAccessDenied):
            action()
    assert not state["writes"] and selected.saver.storage == before
    selected.model_factory = lambda **_: continuation.Model(AIMessage(content="Approved."))
    result = continuation.run(selected, grant=grant)
    assert result.execution["receipts"] and len(state["writes"]) == 1


@pytest.mark.parametrize("operation", ["card", "delete", "discard"])
def test_authenticated_http_still_rejects_a_foreign_conversation(operation, host_identity):
    selected = runtime(InMemorySaver(), Capture())
    selected.update_state(config(), {"messages": [HumanMessage(content="PRIVATE_CANARY")]})
    application = create_app()
    application.state.runtime = selected

    async def run(function, *args):
        return await asyncio.to_thread(function, *args)

    application.state.workers = SimpleNamespace(closing=False, run=run)
    before = deepcopy(selected.saver.storage)

    async def request():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=application),
                                     base_url="http://fixture") as client:
            if operation == "card":
                return await client.post("/tasks/confirmation", headers=host_identity(8),
                    json={"user_id": 7, "conversation_id": 42})
            if operation == "delete":
                return await client.delete("/checkpoint/42?uid=8",
                                           headers=cleanup_headers(42, uid=8))
            return await client.post("/checkpoint/42/drop_turn",
                headers=cleanup_headers(42, uid=8, turn_id="old"),
                json={"uid": 8, "turn_id": "old"})

    response = asyncio.run(request())
    assert response.status_code == 404
    assert response.json() == {"detail": "conversation_not_found"}
    assert "PRIVATE_CANARY" not in response.text and selected.saver.storage == before


@pytest.mark.parametrize("stream,resume", [
    (False, False), (False, True), (True, False), (True, True),
])
def test_chat_http_and_sse_reject_foreign_state_without_calling_model(
    stream, resume, host_identity, service_workers, monkeypatch
):
    from agent.main import app

    model = Capture()
    selected = runtime(InMemorySaver(), model)
    selected.update_state(config(), {"messages": [HumanMessage(content="PRIVATE_CANARY")]})
    monkeypatch.setattr(app.state, "runtime", selected)
    before = deepcopy(selected.saver.storage)

    async def request():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://fixture") as client:
            return await client.post("/chat/stream/v2" if stream else "/chat",
                headers=host_identity(8), json={"user_id": 7, "conversation_id": 42,
                    "message": "", "resume_task": "kept"} if resume else {
                    "user_id": 7, "conversation_id": 42, "message": "continue"})

    response = asyncio.run(request())
    if stream:
        assert response.status_code == 200
        assert 'event: error\ndata: "conversation_not_found"' in response.text
        assert "event: done" not in response.text
    else:
        assert response.status_code == 404
        assert response.json() == {"detail": "conversation_not_found"}
    assert "PRIVATE_CANARY" not in response.text
    assert not model.prompts and selected.saver.storage == before
