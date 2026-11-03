"""A failed confirmation returns to the Agent without authorizing another write."""

import json

import httpx
import test_confirmation_continuation as continuation
from langchain_core.messages import AIMessage, ToolMessage
from test_confirmation_continuation import (
    Model,
    call,
    config,
    prepare,
    run,
    runtime,
)

from agent import confirm
from agent.products.blog.confirmation import cancel
from tools import base

backend = continuation.backend


def test_unknown_response_returns_to_readback_and_does_not_dispatch_siblings(backend, monkeypatch):
    state, _ = backend
    selected = runtime(Model())
    _, grant, _ = prepare(selected)
    client = base._client

    def lose_response(request):
        response = client.send(request)
        if request.method == "POST" and request.url.path.endswith("/favorites"):
            raise httpx.ReadTimeout("synthetic lost response", request=request)
        return response

    model = Model(AIMessage(content="", tool_calls=[call("list_my_favorites", "verify")]),
                  AIMessage(content="已查到文章19在收藏中，后续通知尚未处理。"))
    selected.model_factory = lambda **_: model
    with httpx.Client(transport=httpx.MockTransport(lose_response)) as transport:
        monkeypatch.setattr(base, "_client", transport)
        done = run(selected, grant=grant)
        assert state["favorites"] == {19} and not state["read"]
        assert len(state["writes"]) == 1 and len(model.prompts) == 2
        assert not done.execution["receipts"] and done.execution["operation_outcome"]
        assert not done.execution.get("pending_confirm")
        facts = [json.loads(message.content) for message in model.prompts[0]
                 if isinstance(message, ToolMessage)
                 and message.tool_call_id in ("favorite", "notice")]
        assert {row["status"] for row in facts} == {"awaiting_confirmation", "not_executed"}
        unknown = next(message for message in model.prompts[0]
                       if isinstance(message, ToolMessage)
                       and message.tool_call_id.startswith("unverified_"))
        assert unknown.status == "error"
        assert json.loads(unknown.content)["status"] == "unverified"
        assert json.loads(unknown.content)["approval"] == "user_confirmed"
        assert not selected.store.load(config()).has_pending
        before = len(state["requests"])
        replay = run(selected, grant=grant)
        assert not replay.execution["receipts"] and len(state["requests"]) == before
        assert len(model.prompts) == 2


def test_first_success_survives_second_approval_and_cancellation(backend):
    state, _ = backend
    selected = runtime(Model())
    _, grant, _ = prepare(selected)
    second = run(selected, grant=grant)
    next_grant = confirm.verify(second.execution["pending_confirm"]["token"], 7, 42)
    assert second.execution["receipts"][0]["tool"] == "add_favorite"
    assert cancel(selected, next_grant, config())["cancelled"]
    assert state["favorites"] == {19} and not state["read"]
    assert len(state["writes"]) == 1
    replay = run(selected, grant=next_grant)
    assert not replay.execution["receipts"] and len(state["writes"]) == 1


def test_two_confirmations_preserve_both_verified_receipts(backend):
    selected = runtime(Model())
    _, first, _ = prepare(selected)
    second = run(selected, grant=first)
    grant = confirm.verify(second.execution["pending_confirm"]["token"], 7, 42)
    selected.model_factory = lambda **_: Model(AIMessage(content="收藏与通知均已完成。"))
    done = run(selected, grant=grant)
    assert [row["tool"] for row in done.execution["receipts"]] == [
        "add_favorite", "read_notifications"]
