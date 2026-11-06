"""Maintenance IDs are validated before any checkpoint or database operation."""

import asyncio
from types import SimpleNamespace

import httpx
import pytest
from _memory_support import Capture, runtime
from langgraph.checkpoint.memory import InMemorySaver

from agent.main import create_app


@pytest.fixture
def application():
    app = create_app()
    model = Capture()
    app.state.runtime = runtime(InMemorySaver(), model)
    calls = []

    async def run(function, *args):
        calls.append((function.__name__, args))
        return function(*args)

    app.state.workers = SimpleNamespace(closing=False, run=run)
    return app, calls, model


def send(app, route, headers):
    async def request():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://test") as client:
            if route.endswith("/drop_turn"):
                return await client.post(route, json={"uid": 7, "turn_id": "probe"},
                                         headers=headers)
            return await client.delete(route + "?uid=7", headers=headers)
    return asyncio.run(request())


@pytest.mark.parametrize("identifier", ["letters", "0", "-1", "1.5", "2147483648"])
@pytest.mark.parametrize("suffix", ["", "/drop_turn"])
def test_invalid_id_is_rejected_without_checkpoint_access(application, host_identity,
                                                          identifier, suffix):
    app, calls, model = application
    response = send(app, f"/checkpoint/{identifier}{suffix}", host_identity())
    assert response.status_code == 422
    assert calls == [] and model.prompts == []


@pytest.mark.parametrize("identifier", [1, 2_147_483_647])
@pytest.mark.parametrize("suffix,field", [("", "deleted"), ("/drop_turn", "dropped")])
def test_valid_id_reaches_existing_maintenance_as_an_integer(application, host_identity,
                                                           identifier, suffix, field):
    app, calls, model = application
    response = send(app, f"/checkpoint/{identifier}{suffix}", host_identity())
    assert response.status_code == 200 and response.json()[field] is True
    assert len(calls) == 1 and type(calls[0][1][1]) is int
    assert calls[0][1][1] == identifier and model.prompts == []


@pytest.mark.parametrize("suffix", ["", "/drop_turn"])
def test_well_formed_unsigned_request_still_requires_host_auth(application, suffix):
    app, calls, model = application
    assert send(app, f"/checkpoint/42{suffix}", {}).status_code == 401
    assert calls == [] and model.prompts == []
