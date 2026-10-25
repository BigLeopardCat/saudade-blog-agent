"""Conversation affinity reaches real SDK requests without mixing concurrent actors."""

import asyncio
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest
from langchain_core.messages import AIMessageChunk
from langgraph.checkpoint.memory import InMemorySaver
from test_memory_boundaries import ACTOR, config, run, runtime

from agent.principal import SOURCE_ASSERTION, SOURCE_BODY, Principal
from agent.products.blog.context import conversation_session
from agent.runtime.control import RequestControl
from agent.runtime.workers import RequestWorkers
from config.settings import settings
from models.llm import get_llm
from models.session import current_session


@pytest.mark.parametrize("principal,cid", [
    (Principal(7, "user", SOURCE_BODY), 42), (ACTOR, None), (ACTOR, True),
    (ACTOR, -1), (Principal(7, None, SOURCE_ASSERTION), 42),
])
def test_unverified_or_missing_coordinates_never_claim_conversation_affinity(principal, cid):
    assert conversation_session(principal, cid) is None


def test_bound_conversation_ids_survive_reconstruction_and_partition_accounts_and_roles():
    first = conversation_session(ACTOR, 42)
    assert first == conversation_session(Principal(7, "user", SOURCE_ASSERTION), 42)
    assert len(first) == 64 and "conv_42" not in first
    assert len({first, conversation_session(ACTOR, 43),
                conversation_session(Principal(8, "user", SOURCE_ASSERTION), 42),
                conversation_session(Principal(7, "admin", SOURCE_ASSERTION), 42)}) == 4


def test_native_turns_reuse_affinity_on_the_actual_sdk_http_wire(monkeypatch):
    monkeypatch.setattr(settings, "llm_provider", "opencode")
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(200, json={
            "id": "fixture", "object": "chat.completion", "created": 0, "model": "fixture",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "seen"},
                         "finish_reason": "stop"}],
        })

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        class Model:
            max_tokens = 64

            def bind_tools(self, *args, **kwargs):
                return self

            def stream(self, messages, **kwargs):
                # Constructing the SDK inside each turn used to generate a new routing ID.
                response = get_llm(api_key="fixture", model="fixture",
                                   base_url="http://fixture/v1", http_client=client,
                                   streaming=False).invoke(messages)
                yield AIMessageChunk(content=response.content)

        from agent.api.schemas import ChatRequest

        saver = InMemorySaver()
        for cfg in (config(), config(), config(Principal(8, "user", SOURCE_ASSERTION))):
            run(runtime(saver, Model()), ChatRequest(message="hello"), cfg)
    headers = [request.headers["x-opencode-session"] for request in requests]
    assert headers[0] == headers[1] == conversation_session(ACTOR, 42)
    assert headers[2] != headers[0]
    assert all(request.headers["user-agent"] == "saudade-blog-agent/1.0" for request in requests)
    assert all("session_id" not in json.loads(request.content) for request in requests)
    assert current_session() is None


def test_auxiliary_worker_jobs_share_the_request_session_and_reset_afterward():
    async def scenario():
        workers = RequestWorkers(max_workers=2)
        loop = asyncio.get_running_loop()
        barrier = threading.Barrier(2)
        sessions = [conversation_session(ACTOR, cid) for cid in (42, 43)]
        groups = [workers.request(loop, lambda: None, control=RequestControl(
            threading.Event(), time.monotonic() + 5, session_id=identifier))
            for identifier in sessions]

        def inspect():
            barrier.wait(timeout=2)
            return current_session()

        try:
            assert await asyncio.gather(*(group.submit(workers.executor, inspect)
                                          for group in groups)) == sessions
            assert await groups[0].submit(workers.executor, current_session) == sessions[0]
        finally:
            await workers.aclose()
        assert current_session() is None

    asyncio.run(scenario())
    with ThreadPoolExecutor(max_workers=1) as pool:
        assert pool.submit(current_session).result() is None
