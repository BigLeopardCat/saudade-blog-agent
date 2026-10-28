"""Framework summaries preserve complete sources and share the primary request budget."""

import asyncio
import json
import secrets
from types import SimpleNamespace

import httpx
import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from agent.api.routes import chat as server
from agent.main import create_app
from agent.principal import SOURCE_ASSERTION, Principal
from agent.runtime.budget import BudgetLimits, RequestBudget
from agent.runtime.control import RequestControl
from agent.runtime.native import NativeRuntime
from agent.runtime.native_store import NativeConversationStore
from agent.runtime.summarization import SummaryUnavailable, summarize
from agent.runtime.workers import RequestWorkers
from models.llm import get_llm

ACTOR = Principal(7, "user", SOURCE_ASSERTION)


def test_summary_receives_the_complete_source_prefix_without_clipping():
    seen = []
    messages = [HumanMessage(content="x" * 650 + "EARLY_TAIL_CANARY")]
    messages.extend(HumanMessage(content=f"source-{number}") for number in range(35))
    model = SimpleNamespace(invoke=lambda prompt, **kwargs: seen.append(prompt) or
                            AIMessage(content="summary", response_metadata={
                                "finish_reason": "stop"}))
    assert summarize(messages, budget=RequestBudget(), config={}, model=model) == "summary"
    assert len(seen) == 1 and isinstance(seen[0][0], SystemMessage)
    assert "EARLY_TAIL_CANARY" in seen[0][1].content and "source-34" in seen[0][1].content
    assert "EARLY_TAIL_CANARY" not in seen[0][0].content


@pytest.mark.parametrize("kind", ["empty", "truncated", "refusal", "tools", "oversized"])
def test_incomplete_summary_is_rejected_without_claiming_coverage(kind):
    response = AIMessage(content="summary")
    if kind == "empty":
        response.content = ""
    elif kind == "truncated":
        response.response_metadata["finish_reason"] = "length"
    elif kind == "refusal":
        response.additional_kwargs["refusal"] = "refused"
    elif kind == "tools":
        response.tool_calls = [{"name": "unused", "args": {}, "id": "one"}]
    else:
        response.content = "字" * 1601
    model = SimpleNamespace(invoke=lambda *args, **kwargs: response)
    with pytest.raises(SummaryUnavailable):
        summarize([HumanMessage(content="source")], budget=RequestBudget(), config={}, model=model)


@pytest.mark.parametrize("limits", [BudgetLimits(context_tokens=1000),
                                    BudgetLimits(model_calls=0)])
def test_oversized_or_unfunded_summary_does_not_call_the_provider(limits):
    budget = RequestBudget(limits)
    model = SimpleNamespace(invoke=lambda *args, **kwargs: pytest.fail("unfunded call"))
    with pytest.raises(SummaryUnavailable):
        summarize([HumanMessage(content="x" * 20000 + "TAIL_CANARY")],
                  budget=budget, config={}, model=model)
    assert budget.model_calls == budget.tokens == 0 and budget.reason is None


@pytest.mark.parametrize("stream", [False, True])
def test_retired_host_hints_schedule_no_second_call_on_actual_http_and_sdk(monkeypatch, stream):
    requests, budgets = [], []

    def handle(request):
        body = json.loads(request.content)
        requests.append(body)
        message = {"role": "assistant", "content": "primary answer"}
        usage = {"prompt_tokens": 20, "completion_tokens": 5, "total_tokens": 25}
        chunk = {"id": "fixture", "model": "fixture", "object": "chat.completion.chunk",
                 "choices": [{"index": 0, "delta": message, "finish_reason": "stop"}],
                 "usage": usage}
        if body.get("stream"):
            return httpx.Response(200, headers={"content-type": "text/event-stream"},
                text="data: " + json.dumps(chunk) + "\n\ndata: [DONE]\n\n")
        return httpx.Response(200, json={"id": "fixture", "model": "fixture",
            "object": "chat.completion", "choices": [
                {"index": 0, "message": message, "finish_reason": "stop"}], "usage": usage})

    def control(*args, **kwargs):
        budget = RequestBudget(BudgetLimits(model_calls=1))
        budgets.append(budget)
        return RequestControl(*args, **kwargs, budget=budget)

    with httpx.Client(transport=httpx.MockTransport(handle)) as model_client:
        def factory(**kwargs):
            return get_llm(model="fixture", api_key=secrets.token_urlsafe(24),
                           base_url="https://model.test/v1", http_client=model_client, **kwargs)

        catalog = SimpleNamespace(initial_tools=lambda _: {}, domains=lambda *_: {})
        selected = NativeRuntime(NativeConversationStore(None), model_factory=factory,
                                 catalog=catalog)
        monkeypatch.setattr(server, "RequestControl", control)
        monkeypatch.setattr(server, "_resolve_principal", lambda *args: ACTOR)

        async def scenario():
            app = create_app()
            app.state.runtime, app.state.workers = selected, RequestWorkers()
            app.state.chat_slots = asyncio.BoundedSemaphore(1)
            try:
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                             base_url="http://test") as client:
                    return await client.post("/chat/stream/v2" if stream else "/chat", json={
                        "message": "current request", "conversation_id": 42,
                        "needs_summary": True, "summary_batch": {"base_id": 0,
                            "messages": [{"id": 10, "role": "user", "content": "old" * 4000}]}})
            finally:
                await app.state.workers.aclose()

        response = asyncio.run(scenario())
    assert response.status_code == 200 and len(requests) == 1
    assert "current request" in str(requests[0]) and "oldoldold" not in str(requests[0])
    if stream:
        assert "event: done\n" in response.text and "event: summary\n" not in response.text
    else:
        assert response.json() == {"reply": "primary answer", "success": True,
                                   "error": None, "executions": []}
    assert budgets[0].model_calls == 1 and budgets[0].tokens == 25
