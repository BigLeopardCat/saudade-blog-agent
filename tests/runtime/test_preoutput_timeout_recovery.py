"""Real SDK transport recovery must not replay output or completed tool work."""

import json
from types import SimpleNamespace

import httpx
import pytest
from _own_support import config
from langchain_core.messages import HumanMessage
from langchain_core.tools import tool
from langgraph.checkpoint.memory import InMemorySaver
from openai import APIConnectionError, APITimeoutError

from agent.products.blog.application import BlogApplication
from agent.runtime.checkpoint_conversations import CheckpointConversationStore
from agent.runtime.service import AgentRuntime
from models.llm import get_llm
from models.transport import ModelIO
from tools.result import ok

pytestmark = pytest.mark.tool_scopes(fixture_read="read.public")


def event(delta):
    return ("data: " + json.dumps({"id": "sample", "choices": [
        {"index": 0, "delta": delta, "finish_reason": None}]}) + "\n\n").encode()


@pytest.mark.parametrize("failure", [httpx.ReadTimeout, httpx.ConnectError,
                                     httpx.ReadError, httpx.RemoteProtocolError])
@pytest.mark.parametrize("prefix,exhausted,attempts,recovered", [
    (None, False, 3, True),
    ({"role": "assistant", "content": ""}, False, 3, True),
    (None, True, 3, False),
    ({"content": "Partial answer"}, False, 2, False),
    ({"reasoning_content": "Partial reasoning"}, False, 2, False),
    ({"tool_calls": [{"index": 0, "id": "partial", "type": "function", "function": {
        "name": "fixture_read", "arguments": "{"}}]}, False, 2, False),
])
def test_transport_recovery_is_bounded_and_preserves_the_completed_read(
        monkeypatch, failure, prefix, exhausted, attempts, recovered):
    monkeypatch.setattr("config.settings.llm_provider", "opencode")
    monkeypatch.setattr("langchain.agents.middleware.model_retry.time.sleep", lambda _: None)
    requests, calls, observations = [], [], []
    monkeypatch.setattr("agent.runtime.middleware.record",
        lambda node, name, **data: observations.append({"event": name, **data}))

    @tool
    def fixture_read() -> object:
        """Read the fixture fact once."""
        calls.append("read")
        return ok("Persisted fact.")

    class Body(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield event(prefix)
            raise failure("Synthetic transport failure")

    async def provider(request):
        requests.append(json.loads(request.content))
        if len(requests) > 1 and (len(requests) == 2 or exhausted):
            if prefix is None:
                raise failure("Synthetic transport failure", request=request)
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=Body())
        delta = ({"tool_calls": [{"index": 0, "id": "read", "type": "function", "function": {
            "name": "fixture_read", "arguments": "{}"}}]} if len(requests) == 1 else
                 {"content": "Recovered the persisted fact."})
        return httpx.Response(200, headers={"content-type": "text/event-stream"},
                              content=event(delta) + b"data: [DONE]\n\n")

    catalog = SimpleNamespace(initial_tools=lambda _: {fixture_read.name: fixture_read},
                              domains=lambda *_: {})
    with ModelIO(transport=httpx.MockTransport(provider)) as io:
        runtime = AgentRuntime(CheckpointConversationStore(InMemorySaver()),
            application=BlogApplication(catalog), model_io=io,
            model_factory=lambda **kwargs: get_llm(api_key="synthetic-only",
                base_url="https://model.test/v1", model="deepseek-v4.1-flash",
                max_retries=0, **kwargs))
        inputs = {"messages": [HumanMessage(content="Read once.")], "task_id": "timeout-read"}
        if recovered:
            outcome = list(runtime.stream_events(inputs, config()))[-1]
            assert outcome.reply == "Recovered the persisted fact."
            assert not runtime.store.load(config()).unfinished
        else:
            with pytest.raises((APIConnectionError, httpx.TransportError)):
                list(runtime.stream_events(inputs, config()))
            assert runtime.store.load(config()).unfinished
    assert len(requests) == attempts and calls == ["read"]
    if attempts == 3:
        assert requests[1] == requests[2]
    errors = [row for row in observations if row["event"] == "llm_error"]
    assert len(errors) == (2 if exhausted else 1)
    assert all(row["failure_source"] == "transport" for row in errors)
    output_started = bool(prefix and (prefix.get("content") or prefix.get("reasoning_content")
                                     or prefix.get("tool_calls")))
    assert all(row["stream_output_started"] is output_started for row in errors)


@pytest.mark.parametrize("failure", [APIConnectionError, APITimeoutError,
                                     httpx.ConnectError, httpx.ReadError,
                                     httpx.RemoteProtocolError])
def test_connection_recovery_requires_explicit_preoutput_evidence(failure):
    from models.errors import retryable_model_request

    error = (failure(request=httpx.Request("POST", "https://model.test/v1"))
             if issubclass(failure, APIConnectionError) else failure("synthetic"))
    assert not retryable_model_request(error)
    assert retryable_model_request(error, before_output=True)
