"""Real SDK cancellation closes stalled responses and preserves the shared client."""

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import anyio
import httpx
import pytest
from langchain_core.messages import HumanMessage

from agent.runtime.control import AgentCancelled, RequestControl, request_scope
from models.llm import get_llm
from models.session import session_scope
from models.transport import ModelIO, io_scope


def event(body):
    return ("data: " + json.dumps({"id": "sample", **body}) + "\n\n").encode()


def answer():
    return (event({"choices": [{"index": 0, "delta": {"content": "Hi"},
                               "finish_reason": None}]})
            + event({"choices": [], "usage": {"prompt_tokens": 20, "completion_tokens": 2,
                    "total_tokens": 22, "prompt_tokens_details": {"cached_tokens": 16}}})
            + b"data: [DONE]\n\n")


class StalledBody(httpx.AsyncByteStream):
    def __init__(self, entered, *, first=False):
        self.entered, self.closed = entered, threading.Event()
        self.first = first

    async def __aiter__(self):
        if self.first:
            yield event({"choices": [{"index": 0, "delta": {"content": "Hi"},
                                      "finish_reason": None}]})
        self.entered.set()
        await anyio.sleep_forever()

    async def aclose(self):
        await anyio.sleep(0.01)
        self.closed.set()


@pytest.mark.parametrize("phase", ["headers", "first_token", "between_tokens", "plain_response"])
@pytest.mark.parametrize("end", ["stop", "deadline"])
def test_interrupt_closes_sdk_io_before_another_server_chunk(monkeypatch, phase, end):
    monkeypatch.setattr("config.settings.llm_provider", "opencode")
    entered, stopped, headers_closed = threading.Event(), threading.Event(), threading.Event()
    body = StalledBody(entered, first=phase == "between_tokens")
    requests, errors = [], []

    async def provider(request):
        requests.append(request)
        if phase in {"headers", "plain_response"}:
            entered.set()
            try:
                await anyio.sleep_forever()
            finally:
                headers_closed.set()
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=body)

    with ModelIO(transport=httpx.MockTransport(provider)) as io:
        def run():
            try:
                with io_scope(io), session_scope("synthetic-session"):
                    model = get_llm(api_key="synthetic-only", base_url="https://model.test/v1",
                                    model="deepseek-v4.1-flash", max_retries=0,
                                    streaming=phase != "plain_response", max_tokens=64)
                    deadline = time.monotonic() + (0.3 if end == "deadline" else 10)
                    with request_scope(RequestControl(stopped, deadline,
                                                       session_id="synthetic-session")):
                        model.invoke([HumanMessage(content="Hello.")])
            except (AgentCancelled, TimeoutError) as exc:
                errors.append(exc)

        worker = threading.Thread(target=run)
        worker.start()
        assert entered.wait(2), "SDK did not reach the held response"
        started = time.monotonic()
        if end == "stop":
            stopped.set()
        worker.join(2)
        assert not worker.is_alive(), "interruption still waits for an upstream chunk"
        assert time.monotonic() - started < 1
        assert len(errors) == 1
        assert isinstance(errors[0], AgentCancelled if end == "stop" else TimeoutError)
        closed = headers_closed if phase in {"headers", "plain_response"} else body.closed
        assert closed.is_set()
        assert len(requests) == 1
        assert requests[0].headers["x-opencode-session"] == "synthetic-session"
        assert not io.client.is_closed, "one task cancellation closed the shared client"


def test_stream_messages_and_usage_reuse_one_client(monkeypatch):
    monkeypatch.setattr("config.settings.llm_provider", "opencode")
    requested = []

    async def provider(request):
        requested.append(json.loads(request.content))
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=answer())

    with ModelIO(transport=httpx.MockTransport(provider)) as io, io_scope(io):
        client = io.client
        with request_scope(RequestControl(threading.Event(), time.monotonic() + 5)):
            model = get_llm(api_key="synthetic-only", base_url="https://model.test/v1",
                            model="deepseek-v4.1-flash", streaming=True, max_tokens=64,
                            stream_usage=True, max_retries=0)
            for _ in range(2):
                chunks = list(model.stream([HumanMessage(content="Hello.")]))
                assert "".join(chunk.content for chunk in chunks) == "Hi"
                aggregate = chunks[0]
                for chunk in chunks[1:]:
                    aggregate += chunk
                assert aggregate.usage_metadata["input_token_details"]["cache_read"] == 16
            assert model.http_async_client is client and not client.is_closed
    assert client.is_closed
    assert len(requested) == 2 and requested[0] == requested[1]


def test_shared_async_pool_keeps_parallel_request_contexts_separate(monkeypatch):
    monkeypatch.setattr("config.settings.llm_provider", "opencode")
    from models.session import current_session

    seen = []

    async def provider(request):
        seen.append((request.headers["x-opencode-session"], current_session()))
        await anyio.sleep(0.02)
        return httpx.Response(200, json={"id": "sample", "choices": [{"index": 0,
            "message": {"role": "assistant", "content": "Hello."}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 1, "total_tokens": 11}})

    with ModelIO(transport=httpx.MockTransport(provider)) as io:
        def run(identifier):
            with io_scope(io), request_scope(RequestControl(threading.Event(),
                    time.monotonic() + 5, session_id=identifier)):
                return get_llm(api_key="synthetic-only", base_url="https://model.test/v1",
                    model="deepseek-v4.1-flash", streaming=False).invoke(
                        [HumanMessage(content="Hello.")]).content

        with ThreadPoolExecutor(max_workers=2) as workers:
            assert list(workers.map(run, ["caller-A", "caller-B"])) == ["Hello.", "Hello."]
    assert sorted(seen) == [("caller-A", "caller-A"), ("caller-B", "caller-B")]
