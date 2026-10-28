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

from agent.runtime.control import AgentCancelled, RequestControl, request_scope
from agent.runtime.native import NativeRuntime
from agent.runtime.native_store import NativeConversationStore
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
            return NativeRuntime(NativeConversationStore(saver), catalog=catalog, model_io=io,
                model_factory=lambda **kwargs: get_llm(api_key="synthetic-only",
                    base_url="https://model.test/v1", model="deepseek-v4.1-flash", **kwargs))

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
        with request_scope(RequestControl(threading.Event(), time.monotonic() + 10)):
            result = list(runtime().stream_events({"resume_task": "io-recover"}, config()))[-1]
        assert result.reply == "Recovered the saved fact."
        assert calls == ["read"] and len(requests) == 3
        assert requests[1] == requests[2]
        assert not runtime().store.load(config()).unfinished
