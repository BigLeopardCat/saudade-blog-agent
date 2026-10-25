"""The service factory and HTTP/SSE routes use one native conversation store."""


import asyncio
from contextlib import ExitStack

import pytest
from _own_support import config
from _service import app
from fastapi import Request
from langchain_core.messages import AIMessageChunk
from langgraph.checkpoint.memory import InMemorySaver

from agent import checkpoint
from agent.api import lifecycle
from agent.api.routes import chat as server
from agent.api.schemas import ChatRequest, HistoryItem
from agent.runtime.native import NativeRuntime
from agent.runtime.native_store import NativeConversationStore
from config.settings import settings


@pytest.mark.parametrize("saver", [None, InMemorySaver()])
def test_default_factory_attaches_the_selected_saver(monkeypatch, saver):
    monkeypatch.setattr(checkpoint, "build_checkpointer", lambda **kwargs: saver)
    monkeypatch.setattr(settings, "agent_arm", "native")
    with ExitStack() as resources:
        runtime, name = lifecycle.build_runtime(resources)
    assert name == "native" and isinstance(runtime, NativeRuntime)
    assert checkpoint.live_saver(runtime) is saver
    assert runtime.store.available is (saver is not None)
    assert not runtime.store.is_resuming(config())


@pytest.mark.parametrize("name", ["graph", "hybrid", "react", "unknown"])
def test_retired_factories_cannot_enter_the_native_transport(monkeypatch, name):
    monkeypatch.setattr(settings, "agent_arm", name)
    with ExitStack() as resources, pytest.raises(RuntimeError, match="AGENT_ARM=native"):
        lifecycle.build_runtime(resources)


@pytest.mark.parametrize("persistent", [False, True])
def test_http_and_sse_share_history_without_duplicates(monkeypatch, persistent):
    prompts = []

    class Model:
        max_tokens = 64

        def bind_tools(self, *args, **kwargs):
            return self

        def stream(self, prompt, **kwargs):
            prompts.append(list(prompt))
            yield AIMessageChunk(content=f"答复{len(prompts)}")

    runtime = NativeRuntime(NativeConversationStore(InMemorySaver() if persistent else None),
                            model_factory=lambda **_: Model())
    monkeypatch.setattr(app.state, "runtime", runtime)
    monkeypatch.setattr(server, "_resolve_principal", lambda request, uid: config()[
        "configurable"]["principal"])
    monkeypatch.setattr(server, "start_trace", lambda *a, **k: None)
    request = Request({"app": app, "type": "http", "method": "POST", "path": "/chat",
                       "headers": [], "query_string": b""})

    async def exercise():
        first = await server.chat(ChatRequest(
            message="第一问", user_id=7, conversation_id=42), request)
        assert first.success and first.reply == "答复1"
        response = await server.chat_stream(ChatRequest(
            message="接着问", user_id=7, conversation_id=42,
            history_scope={"user_id": 7, "role": "user", "conversation_id": 42},
            history=[HistoryItem(role="user", content="[当前问题]: 第一问"),
                     HistoryItem(role="assistant", content="答复1")]), request)
        body = "".join([frame async for frame in response.body_iterator])
        assert "答复2" in body and "event: done" in body and "event: error" not in body

    asyncio.run(exercise())
    assert len(prompts) == 2
    assert sum(m.content == "答复1" for m in prompts[1]) == 1
    if persistent:
        saved = runtime.store.load(config()).messages
        assert [m.content for m in saved] == [
            "[当前问题]: 第一问", "答复1", "[当前问题]: 接着问", "答复2"]
        assert runtime.get_state(config()).next == ()
    else:
        assert runtime.store.load(config()).messages == []


pytestmark = pytest.mark.usefixtures("service_workers")
