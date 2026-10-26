"""Optional summaries use complete, scoped batches and never take the primary call."""

import asyncio
import json
import secrets
from types import SimpleNamespace

import httpx
import pytest
from langchain_core.messages import HumanMessage
from pydantic import ValidationError

from agent import summarizer
from agent.api.routes import chat as server
from agent.api.schemas import ChatRequest
from agent.main import create_app
from agent.principal import SOURCE_ASSERTION, Principal
from agent.runtime.budget import BudgetLimits, RequestBudget, budget_scope
from agent.runtime.control import RequestControl
from agent.runtime.memory_scope import safe_request
from agent.runtime.native import NativeRuntime
from agent.runtime.native_store import NativeConversationStore
from agent.runtime.workers import RequestWorkers
from models.llm import get_llm

ACTOR = Principal(7, "user", SOURCE_ASSERTION)
SCOPE = {"user_id": 7, "role": "user", "conversation_id": 42}


def payload(**updates):
    return {"message": "current request", "conversation_id": 42, "needs_summary": True,
            "history_scope": SCOPE,
            "summary_batch": {"base_id": 0, "messages": [
                {"id": 10, "role": "user", "content": "Earlier request"},
                {"id": 12, "role": "assistant", "content": "Earlier answer"}]}, **updates}


@pytest.mark.parametrize("ids,base", [([12, 10], 0), ([10, 10], 0), ([10], 10), ([0], 0),
                                         ([True], 0), (["10"], 0)])
def test_summary_range_requires_ordered_unique_ids_after_its_base(ids, base):
    with pytest.raises(ValidationError):
        ChatRequest(**payload(summary_batch={"base_id": base, "messages": [
            {"id": ident, "role": "user", "content": "message"} for ident in ids]}))


def test_legacy_or_mismatched_sources_cannot_schedule_summary_or_forge_a_base():
    for changes in ({"summary_batch": None}, {"history_scope": None},
                    {"history_scope": {**SCOPE, "role": "admin"}},
                    {"summary": "unknown source"}, {"confirm_token": secrets.token_urlsafe(24)},
                    {"summary_batch": {"base_id": 5, "messages": [
                        {"id": 10, "role": "user", "content": "new"}]}}):
        assert not safe_request(ChatRequest(**payload(**changes)), ACTOR).needs_summary
    valid = payload(summary="old", summary_scope=SCOPE,
                    summary_batch={"base_id": 5, "messages": [
                        {"id": 10, "role": "user", "content": "new"}]})
    assert safe_request(ChatRequest(**valid), ACTOR).needs_summary
    assert safe_request(ChatRequest(**payload()), ACTOR).needs_summary
    for actor in (Principal(8, "user", SOURCE_ASSERTION),
                  Principal(7, "admin", SOURCE_ASSERTION)):
        assert not safe_request(ChatRequest(**valid), actor).needs_summary


def test_summary_reads_the_entire_uncovered_batch_and_long_message_tail():
    messages = [{"id": i + 1, "role": "user", "content": f"item-{i}"} for i in range(35)]
    messages[0]["content"] = "x" * 650 + " EARLY_TAIL_CANARY"
    req = safe_request(ChatRequest(**payload(summary_batch={"base_id": 0, "messages": messages},
                                           history=[{"role": "user", "content": "RECENT_ONLY"}])),
                       ACTOR)
    seen = []
    model = SimpleNamespace(invoke=lambda prompt: seen.append(prompt) or SimpleNamespace(
        content="new summary", response_metadata={}))
    assert summarizer.summarize("", req.summary_batch.messages, "", llm=model) == "new summary"
    assert "EARLY_TAIL_CANARY" in seen[0] and "item-34" in seen[0]
    assert "200字" in seen[0] and "不按轮次复述" in seen[0]
    assert "RECENT_ONLY" not in seen[0] and "current request" not in seen[0]
    assert req.summary_batch.through_id == 35


def test_insufficient_optional_budget_skips_without_poisoning_or_calling_provider():
    budget = RequestBudget(BudgetLimits(model_calls=1))
    budget.reserve([HumanMessage(content="primary")], output_limit=8)
    model = SimpleNamespace(invoke=lambda _: pytest.fail("optional call reached provider"))
    with budget_scope(budget):
        assert summarizer.summarize("", [], "", llm=model) == ""
    assert budget.model_calls == 1 and budget.reason is None


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("model_calls,finish,base_id", [
    (1, "stop", 0), (2, "stop", 0), (2, "stop", 5),
    (2, "length", 0), (2, "content_filter", 0)])
def test_real_sdk_primary_precedes_summary_and_both_transports_report_exact_coverage(
        monkeypatch, stream, model_calls, finish, base_id):
    requests, budgets = [], []

    def handle(request):
        body = json.loads(request.content)
        requests.append(body)
        text = "primary answer" if len(requests) == 1 else "new summary"
        message = {"role": "assistant", "content": text}
        usage = {"prompt_tokens": 20, "completion_tokens": 5, "total_tokens": 25}
        if body.get("stream"):
            chunk = {"id": "fixture", "model": "fixture", "object": "chat.completion.chunk",
                     "choices": [{"index": 0, "delta": message, "finish_reason": "stop"}],
                     "usage": usage}
            return httpx.Response(200, headers={"content-type": "text/event-stream"},
                                  text="data: " + json.dumps(chunk) + "\n\ndata: [DONE]\n\n")
        return httpx.Response(200, json={"id": "fixture", "model": "fixture",
            "object": "chat.completion", "choices": [
                {"index": 0, "message": message, "finish_reason": finish}], "usage": usage})

    def control(*args, **kwargs):
        budget = RequestBudget(BudgetLimits(model_calls=model_calls))
        budgets.append(budget)
        return RequestControl(*args, **kwargs, budget=budget)

    with httpx.Client(transport=httpx.MockTransport(handle)) as model_client:
        def factory(**kwargs):
            return get_llm(model="fixture", api_key=secrets.token_urlsafe(24),
                           base_url="https://model.test/v1", http_client=model_client, **kwargs)

        catalog = SimpleNamespace(initial_tools=lambda _: {}, domains=lambda *_: {})
        runtime = NativeRuntime(NativeConversationStore(None), model_factory=factory,
                                catalog=catalog)
        monkeypatch.setattr(server, "RequestControl", control)
        monkeypatch.setattr(server, "_resolve_principal", lambda *args: ACTOR)
        monkeypatch.setattr("models.get_llm", factory)

        async def scenario():
            app = create_app()
            app.state.runtime, app.state.workers = runtime, RequestWorkers()
            app.state.chat_slots = asyncio.BoundedSemaphore(1)
            try:
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                             base_url="http://test") as client:
                    return await client.post("/chat/stream/v2" if stream else "/chat",
                                             json=payload(
                                                 summary="previous summary" if base_id else "",
                                                 summary_scope=SCOPE if base_id else None,
                                                 summary_batch={"base_id": base_id, "messages": [
                                                     {"id": 10, "role": "user",
                                                      "content": "Earlier request"},
                                                     {"id": 12, "role": "assistant",
                                                      "content": "Earlier answer"}]}))
            finally:
                await app.state.workers.aclose()

        response = asyncio.run(scenario())
    assert response.status_code == 200 and len(requests) == model_calls
    assert "current request" in str(requests[0])
    if model_calls == 2:
        assert "Earlier request" in str(requests[1])
        assert "current request" not in str(requests[1])
    updated = model_calls == 2 and finish == "stop"
    if stream:
        assert "primary answer" in response.text
        assert ("event: summary\n" in response.text) is updated
        if updated:
            frames = response.text.split("\n\n")
            frame = next(frame for frame in frames if "event: summary\n" in frame)
            summary = json.loads(frame.split("data: ", 1)[1])
            assert summary == {"text": "new summary", "base_id": base_id, "through_id": 12}
    else:
        assert response.json()["reply"] == "primary answer" and response.json()["success"]
        expected = {"base_id": base_id, "through_id": 12} if updated else None
        assert response.json()["summary_coverage"] == expected
        assert response.json()["new_summary"] == ("new summary" if updated else None)
    assert budgets[0].model_calls == model_calls and budgets[0].tokens == 25 * model_calls
    assert budgets[0].reason is None


@pytest.mark.parametrize("kind", ["budget", "empty", "truncated", "failure", "refusal", "tools"])
def test_summary_failure_does_not_return_a_new_coverage_boundary(kind):
    calls = []
    response = SimpleNamespace(content="new text", response_metadata={})

    def invoke(prompt):
        calls.append(prompt)
        if kind == "failure":
            raise RuntimeError("provider unavailable")
        return response

    if kind == "refusal":
        response.additional_kwargs = {"refusal": "refused"}
    if kind == "tools":
        response.tool_calls = [{"name": "unused", "args": {}, "id": "one"}]
    if kind == "empty":
        response.content = ""
    if kind == "truncated":
        response.response_metadata = {"finish_reason": "length"}
    budget = RequestBudget(BudgetLimits(model_calls=0 if kind == "budget" else 1))
    with budget_scope(budget):
        assert summarizer.summarize("", [], "old", llm=SimpleNamespace(invoke=invoke)) == ""
    assert bool(calls) is (kind != "budget")
    assert budget.reason is None


def test_oversized_complete_batch_is_skipped_without_clipping_away_its_tail():
    req = ChatRequest(**payload(summary_batch={"base_id": 0, "messages": [
        {"id": 10, "role": "user", "content": "x" * 20000 + "TAIL_CANARY"}]}))
    budget = RequestBudget(BudgetLimits(context_tokens=1000))
    model = SimpleNamespace(invoke=lambda _: pytest.fail("oversized batch reached provider"))
    assert "TAIL_CANARY" in summarizer.build_prompt("", req.summary_batch.messages, "")
    with budget_scope(budget):
        assert summarizer.summarize("", req.summary_batch.messages, "", llm=model) == ""
    assert budget.model_calls == budget.tokens == 0 and budget.reason is None


def test_summary_batch_limit_refuses_omission_instead_of_taking_a_recent_suffix():
    messages = [{"id": i + 1, "role": "user", "content": str(i)} for i in range(61)]
    with pytest.raises(ValidationError):
        ChatRequest(**payload(summary_batch={"base_id": 0, "messages": messages}))
    accepted = ChatRequest(**payload(summary_batch={"base_id": 0, "messages": messages[:60]}))
    assert accepted.summary_batch.through_id == 60


def test_remaining_request_tokens_can_skip_summary_without_spending_a_call():
    budget = RequestBudget(BudgetLimits(request_tokens=1000))
    ticket = budget.reserve([HumanMessage(content="primary")], output_limit=8)
    from langchain_core.messages import AIMessage

    budget.settle(ticket, AIMessage(content="done", usage_metadata={
        "input_tokens": 899, "output_tokens": 1, "total_tokens": 900}))
    model = SimpleNamespace(invoke=lambda _: pytest.fail("summary consumed unavailable tokens"))
    with budget_scope(budget):
        assert summarizer.summarize("", [], "old", llm=model) == ""
    assert budget.tokens == 900 and budget.model_calls == 1 and budget.reason is None
