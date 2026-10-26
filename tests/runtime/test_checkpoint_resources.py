"""Checkpoint pools belong to resource scopes, including concurrent application lifespans."""

import asyncio
from contextlib import ExitStack
from types import SimpleNamespace

import httpx
import pytest
from langchain_core.messages import AIMessageChunk
from langgraph.checkpoint.memory import InMemorySaver

from agent import checkpoint
from agent.api import lifecycle
from agent.main import create_app
from agent.runtime import native
from config.settings import settings


class Pool:
    def __init__(self, dsn):
        self.dsn, self.close_count = dsn, 0

    def close(self):
        self.close_count += 1
        assert self.close_count == 1


class Saver(InMemorySaver):
    def __init__(self, pool):
        super().__init__()
        self.pool = pool

    def get_tuple(self, config):
        assert not self.pool.close_count, "read used a closed pool"
        return super().get_tuple(config)

    def put(self, *args, **kwargs):
        assert not self.pool.close_count, "write used a closed pool"
        return super().put(*args, **kwargs)


@pytest.fixture
def persistence(monkeypatch):
    pools = []

    def make_pool(dsn):
        pool = Pool(dsn)
        pools.append(pool)
        return pool

    original_import = checkpoint.importlib.import_module

    def load(name, *args, **kwargs):
        if name == checkpoint._PG_MOD:
            return SimpleNamespace(PostgresSaver=Saver)
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(checkpoint, "_make_pool", make_pool)
    monkeypatch.setattr(checkpoint.importlib, "import_module", load)
    monkeypatch.setattr(checkpoint, "_setup_under_lock", lambda *args, **kwargs: None)
    return pools


@pytest.mark.parametrize("failure", ["construct", "setup"])
def test_failed_creation_closes_only_its_pool_and_can_retry(persistence, monkeypatch, failure):
    def fail(*args, **kwargs):
        raise ValueError("startup fixture")

    with ExitStack() as healthy, ExitStack() as retry:
        first = checkpoint.build_checkpointer("postgresql://localhost/one", resources=healthy)
        with monkeypatch.context() as failing:
            if failure == "construct":
                failing.setattr(Saver, "__init__", fail)
            else:
                failing.setattr(checkpoint, "_setup_under_lock", fail)
            with pytest.raises(ValueError, match="startup fixture"):
                checkpoint.build_checkpointer("postgresql://localhost/two", resources=retry)
            assert [pool.close_count for pool in persistence] == [0, 1]
        second = checkpoint.build_checkpointer("postgresql://localhost/two", resources=retry)
        assert first.pool is persistence[0] and second.pool is persistence[2]
        retry.close()
        retry.close()
        assert [pool.close_count for pool in persistence] == [0, 1, 1]
        assert first.get_tuple({"configurable": {"thread_id": "still_open"}}) is None
    assert [pool.close_count for pool in persistence] == [1, 1, 1]


@pytest.mark.parametrize("second_dsn", ["postgresql://localhost/one", "postgresql://localhost/two"])
def test_concurrent_persistent_applications_own_their_pools(persistence, monkeypatch, second_dsn):
    class Model:
        max_tokens = 32

        def bind_tools(self, *args, **kwargs):
            return self

        def stream(self, prompt, **kwargs):
            yield AIMessageChunk(content="reply")

    monkeypatch.setattr(settings, "agent_require_assertion", False)
    monkeypatch.setattr(lifecycle, "setup_logging", lambda: None)
    monkeypatch.setattr(lifecycle.wordgraph, "warm", lambda: None)
    monkeypatch.setattr(lifecycle.rag_search, "warm", lambda: None)
    monkeypatch.setattr(native, "get_llm", lambda **kwargs: Model())
    first, second = create_app(), create_app()

    async def run():
        monkeypatch.setattr(settings, "checkpoint_dsn", "postgresql://localhost/one")
        one = first.router.lifespan_context(first)
        await one.__aenter__()
        try:
            monkeypatch.setattr(settings, "checkpoint_dsn", second_dsn)
            async with second.router.lifespan_context(second):
                assert first.state.runtime.saver is not second.state.runtime.saver
                assert [pool.dsn for pool in persistence] == [
                    "postgresql://localhost/one",
                    second_dsn,
                ]
                assert [pool.close_count for pool in persistence] == [0, 0]
                await one.__aexit__(None, None, None)
                assert [pool.close_count for pool in persistence] == [1, 0]
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=second), base_url="http://test"
                ) as client:
                    for _ in range(2):
                        response = await client.post(
                            "/chat", json={"message": "hello", "user_id": 7, "conversation_id": 42}
                        )
                        assert response.status_code == 200 and response.json()["reply"] == "reply"
                saved = second.state.runtime.get_state({"configurable": {"thread_id": "conv_42"}})
                assert len(saved.values["messages"]) == 4
        finally:
            await one.__aexit__(None, None, None)
        assert [pool.close_count for pool in persistence] == [1, 1]
        assert first.state.runtime is None and second.state.runtime is None

    asyncio.run(run())
