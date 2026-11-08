"""Current approval controls are authenticated checkpoint projections, not local drafts."""

import asyncio
import secrets
import time
from types import SimpleNamespace

import httpx
import jwt
import test_confirmation_continuation as continuation
from _own_support import config

from agent.main import create_app
from agent.products.blog.confirmation import cancel, public_card
from config.settings import settings

backend = continuation.backend


def test_current_card_is_readable_under_turn_lock_and_disappears_after_revocation(
    backend, monkeypatch
):
    state, _ = backend
    selected = continuation.runtime(continuation.Model())
    prepared, grant, _ = continuation.prepare(selected, continuation.FAVORITE)
    task = selected.store.load(config()).run["id"]
    application = create_app()
    application.state.runtime = selected

    async def run(function, *args):
        return await asyncio.to_thread(function, *args)

    application.state.workers = SimpleNamespace(closing=False, run=run)

    monkeypatch.setattr(
        settings, "agent_assertion_secret", "confirmation-status-assertion-test-secret"
    )

    def headers(uid=7, role="user"):
        token = jwt.encode(
            {"sub": uid, "role": role, "aud": "agent", "exp": int(time.time()) + 60},
            settings.agent_assertion_secret,
            algorithm="HS256",
        )
        return {"X-Agent-Assertion": token}

    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=application), base_url="http://fixture"
        ) as client:
            payload = {"user_id": 999, "conversation_id": 42}
            with selected.conversation_guard(config()):
                response = await asyncio.wait_for(
                    client.post("/tasks/confirmation", json=payload, headers=headers()), timeout=2
                )
            assert response.status_code == 200
            assert response.headers["cache-control"] == "no-store"
            body = response.json()
            assert body["task_id"] == task and body["confirmation"]["task_id"] == task
            assert body["confirmation"]["token"] == prepared.execution["pending_confirm"]["token"]
            assert set(body["confirmation"]) == {
                "id",
                "task_id",
                "q",
                "opts",
                "token",
                "exp",
                "replay_safe",
                "server_time",
                "article_ids_to_refresh",
                "notices",
            }
            assert body["confirmation"]["notices"] == (
                prepared.execution["pending_confirm"]["notices"])
            assert body["confirmation"]["article_ids_to_refresh"] == [19]
            assert not state["writes"]
            for uid, role, cid in [(8, "user", 42), (7, "admin", 42), (7, "user", 43)]:
                value = await client.post(
                    "/tasks/confirmation",
                    json={**payload, "conversation_id": cid},
                    headers=headers(uid, role),
                )
                if uid != 7:
                    assert value.status_code == 404
                    assert value.json() == {"detail": "conversation_not_found"}
                else:
                    assert value.status_code == 200
                    assert value.json()["confirmation"] is None
                assert body["confirmation"]["token"] not in value.text
            mismatch = await client.post(
                "/tasks/confirmation", json={**payload, "task_id": "other-task"}, headers=headers()
            )
            assert mismatch.json() == {"task_id": "other-task", "confirmation": None}
            assert (await client.post("/tasks/confirmation", json=payload)).status_code == 401
            assert cancel(selected, grant, config())["cancelled"]
            closed = await client.post("/tasks/confirmation", json=payload, headers=headers())
            assert closed.json()["confirmation"] is None and not state["writes"]

    asyncio.run(scenario())


def test_display_fallback_id_is_stable_and_excludes_private_execution_fields(monkeypatch):
    monkeypatch.setattr("agent.products.blog.confirmation.time.time", lambda: 100)
    opaque = secrets.token_urlsafe(24)
    value = {
        "pending_confirm": {
            "token": opaque,
            "q": "确认？",
            "opts": [],
            "specs": [{"private": "data"}],
            "exp": 123,
        }
    }
    before, after = public_card(value), public_card(value)
    assert before == after and before["id"]
    assert "specs" not in before and opaque not in before["id"]
