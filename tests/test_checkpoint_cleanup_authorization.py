"""Cleanup capabilities cannot be reused for another owner, target, operation or API."""

import asyncio
from types import SimpleNamespace

import httpx
import jwt
import pytest
from _service import cleanup_headers

from agent import checkpoint
from agent.main import create_app


def send(app, headers, *, turn_id=None, cid=42, uid=7):
    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app),
                                     base_url="http://test") as client:
            if turn_id is not None:
                return await client.post(f"/checkpoint/{cid}/drop_turn", headers=headers,
                                         json={"uid": uid, "turn_id": turn_id})
            return await client.delete(f"/checkpoint/{cid}?uid={uid}", headers=headers)
    return asyncio.run(run())


@pytest.fixture
def application(monkeypatch):
    app = create_app()
    calls = []

    async def run(fn, *args):
        calls.append((fn.__name__, args))
        if fn.__name__ == "delete_thread":
            return True
        return {"dropped": True, "why": "absent"}

    app.state.runtime = object()
    app.state.workers = SimpleNamespace(closing=False, run=run)
    monkeypatch.setattr(checkpoint, "live_saver", lambda _: object())
    return app, calls


@pytest.mark.parametrize("turn", [None, "turn-2"])
def test_ordinary_chat_identity_never_authorizes_checkpoint_cleanup(
        application, host_identity, turn):
    app, calls = application
    assert send(app, host_identity(), turn_id=turn).status_code == 401
    assert calls == []


@pytest.mark.parametrize("changes,target", [
    ({"uid": 8}, {}),
    ({"conversation_id": 43}, {}),
    ({"turn_id": "other"}, {"turn_id": "turn-2"}),
    ({}, {"turn_id": "turn-2"}),
    ({"turn_id": "turn-2"}, {}),
    ({"exp": 1}, {}),
    ({"aud": "agent"}, {}),
    ({"checkpoint": {"conversation_id": True, "operation": "delete_conversation"}}, {}),
])
def test_invalid_or_mismatched_capability_never_reaches_checkpoint(
        application, host_identity, changes, target):
    app, calls = application
    headers = cleanup_headers(**{"conversation_id": 42, **changes})
    assert send(app, headers, **target).status_code in (401, 403)
    assert calls == []


@pytest.mark.parametrize("turn", [None, "turn-2"])
def test_exact_capability_remains_retryable_when_business_target_is_already_deleted(
        application, host_identity, turn):
    app, calls = application
    headers = cleanup_headers(42, turn_id=turn)
    for _ in range(2):
        response = send(app, headers, turn_id=turn)
        assert response.status_code == 200
        assert response.json()["deleted" if turn is None else "dropped"] is True
    assert len(calls) == 2
    assert all(args[1] == 42 for _, args in calls)


def test_modified_signature_cannot_change_target(application, host_identity):
    app, calls = application
    headers = cleanup_headers(42)
    token = headers["X-Agent-Assertion"]
    header, payload, signature = token.split(".")
    claims = jwt.decode(token, options={"verify_signature": False})
    claims["checkpoint"]["conversation_id"] = 43
    forged_payload = jwt.encode(claims, "wrong-key-for-offline-tampering-test").split(".")[1]
    headers["X-Agent-Assertion"] = f"{header}.{forged_payload}.{signature}"
    assert send(app, headers, cid=43).status_code == 401
    assert calls == []


def test_cleanup_capability_is_not_a_chat_or_console_identity(application, host_identity):
    from agent.api.authentication import verify_assertion_claims

    token = cleanup_headers(42)["X-Agent-Assertion"]
    assert verify_assertion_claims(token) is None
