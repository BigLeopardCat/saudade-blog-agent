"""A cancelled real SDK request resumes the same Agent checkpoint without another read."""

import json
import threading
import time
from contextlib import nullcontext
from types import SimpleNamespace

import anyio
import httpx
import pytest
from _own_support import config
from langchain_core.messages import HumanMessage
from langchain_core.tools import tool
from langgraph.checkpoint.memory import InMemorySaver

from agent.products.blog.application import BlogApplication
from agent.runtime.checkpoint_conversations import CheckpointConversationStore
from agent.runtime.control import AgentCancelled, RequestControl, request_scope
from agent.runtime.service import AgentRuntime
from models.llm import get_llm
from models.transport import ModelIO
from tools.result import ok


@pytest.mark.parametrize("controlled", [True, False])
def test_sdk_cancel_preserves_completed_read_and_retry_wire(monkeypatch, controlled):
    monkeypatch.setattr("config.settings.llm_provider", "opencode")
    entered, closed, stopped = threading.Event(), threading.Event(), threading.Event()
    requests, calls, errors = [], [], []

    @tool
    def fixture_read() -> object:
        """Read the fixture fact."""
        calls.append("read")
        return ok("Saved fact.")

    class Body(httpx.AsyncByteStream):
        async def __aiter__(self):
            entered.set()
            await anyio.sleep_forever()
            yield b""

        async def aclose(self):
            await anyio.sleep(0.01)
            closed.set()

    def event(delta):
        return ("data: " + json.dumps({"id": "sample", "choices": [
            {"index": 0, "delta": delta, "finish_reason": None}]}) + "\n\n").encode()

    async def provider(request):
        requests.append(json.loads(request.content))
        if len(requests) == 2:
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=Body())
        delta = ({"tool_calls": [{"index": 0, "id": "read", "type": "function", "function": {
            "name": "fixture_read", "arguments": "{}"}}]} if len(requests) == 1 else
                 {"content": "Recovered the saved fact."})
        return httpx.Response(200, headers={"content-type": "text/event-stream"},
                              content=event(delta) + b"data: [DONE]\n\n")

    saver = InMemorySaver()
    catalog = SimpleNamespace(initial_tools=lambda _: {fixture_read.name: fixture_read},
                              domains=lambda *_: {})
    with ModelIO(transport=httpx.MockTransport(provider)) as io:
        def runtime():
            return AgentRuntime(
                CheckpointConversationStore(saver),
                application=BlogApplication(catalog),
                model_io=io,
                model_factory=lambda **kwargs: get_llm(
                    api_key="synthetic-only",
                    base_url="https://model.test/v1",
                    model="deepseek-v4.1-flash",
                    **kwargs,
                ),
            )

        def run():
            try:
                scope = (request_scope(RequestControl(stopped, time.monotonic() + 10))
                         if controlled else nullcontext())
                with scope:
                    inputs = {"messages": [HumanMessage(content="Read once.")],
                              "task_id": "io-recover"}
                    list(runtime().stream_events(inputs, config(stop_event=stopped)))
            except AgentCancelled as exc:
                errors.append(exc)

        worker = threading.Thread(target=run)
        worker.start()
        assert entered.wait(3), "second model call was not reached"
        stopped.set()
        worker.join(2)
        assert not worker.is_alive() and closed.is_set() and len(errors) == 1
        assert runtime().store.load(config()).unfinished
        scope = (request_scope(RequestControl(threading.Event(), time.monotonic() + 10))
                 if controlled else nullcontext())
        with scope:
            result = list(runtime().stream_events({"resume_task": "io-recover"}, config()))[-1]
        assert result.reply == "Recovered the saved fact."
        assert calls == ["read"] and len(requests) == 3
        assert requests[1] == requests[2]
        assert not runtime().store.load(config()).unfinished


@pytest.mark.parametrize("retry_after,failed_requests", [(None, 2), ("15", 1)])
def test_provider_failure_emits_saved_pause_and_resumes_without_repeating_read(
        monkeypatch, retry_after, failed_requests):
    from openai import RateLimitError

    from agent.runtime.task_inputs import TaskInputEvent

    monkeypatch.setattr("config.settings.llm_provider", "opencode")
    monkeypatch.setattr("langchain.agents.middleware.model_retry.time.sleep", lambda _: None)
    requests, calls = [], []
    failing = True

    @tool
    def fixture_read() -> object:
        """Read the fixture fact once."""
        calls.append("read")
        return ok("Persisted fact.")

    def event(delta):
        return ("data: " + json.dumps({"id": "sample", "choices": [
            {"index": 0, "delta": delta, "finish_reason": None}]}) + "\n\n").encode()

    async def provider(request):
        requests.append(json.loads(request.content))
        if len(requests) > 1 and failing:
            return httpx.Response(429, headers={"retry-after": retry_after} if retry_after else {},
                                  json={"error": {"type": "server_error",
                                                  "message": "PRIVATE_ERROR"}})
        delta = ({"tool_calls": [{"index": 0, "id": "read", "type": "function", "function": {
            "name": "fixture_read", "arguments": "{}"}}]} if len(requests) == 1 else
                 {"content": "Recovered the persisted fact."})
        return httpx.Response(200, headers={"content-type": "text/event-stream"},
                              content=event(delta) + b"data: [DONE]\n\n")

    saver = InMemorySaver()
    catalog = SimpleNamespace(initial_tools=lambda _: {fixture_read.name: fixture_read},
                              domains=lambda *_: {})
    cfg = config(task_inputs=True)
    with ModelIO(transport=httpx.MockTransport(provider)) as io:
        def runtime():
            return AgentRuntime(CheckpointConversationStore(saver),
                application=BlogApplication(catalog), model_io=io,
                model_factory=lambda **kwargs: get_llm(api_key="synthetic-only",
                    base_url="https://model.test/v1", model="deepseek-v4.1-flash",
                    max_retries=0, **kwargs))

        selected = runtime()
        emitted = []
        with pytest.raises(RateLimitError):
            for item in selected.stream_events({"messages": [HumanMessage(content="Read once.")],
                                                 "task_id": "provider-recovery"}, cfg):
                emitted.append(item)
        assert len(requests) == 1 + failed_requests and calls == ["read"]
        assert selected.store.load(cfg).unfinished
        assert any(isinstance(item, TaskInputEvent) and item.data.get("phase") == "paused"
                   and item.data["task_id"] == "provider-recovery" for item in emitted)
        failing = False
        outcome = list(runtime().stream_events({"resume_task": "provider-recovery"}, cfg))[-1]
        assert outcome.reply == "Recovered the persisted fact."
        assert calls == ["read"] and len(requests) == 2 + failed_requests
        assert requests[1] == requests[-1]
        assert not selected.store.load(cfg).unfinished
