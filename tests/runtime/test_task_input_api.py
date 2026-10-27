"""Task input controls use signed identity without entering the held turn lock."""

import asyncio
import time
from types import SimpleNamespace

import httpx
import jwt
from langgraph.checkpoint.memory import InMemorySaver

from agent.main import create_app
from agent.principal import SOURCE_ASSERTION, Principal
from agent.runtime.native import NativeRuntime
from agent.runtime.native_store import NativeConversationStore
from agent.runtime.task_inputs import binding
from config.settings import settings


def test_signed_control_is_available_while_conversation_is_locked(monkeypatch):
    monkeypatch.setattr(settings, "jwt_secret", "task-input-test-secret-does-not-leave-the-test")
    monkeypatch.setattr(settings, "agent_require_assertion", True)
    application = create_app()
    selected = NativeRuntime(NativeConversationStore(InMemorySaver()))
    application.state.runtime = selected

    async def run(function, *args):
        return await asyncio.to_thread(function, *args)

    application.state.workers = SimpleNamespace(closing=False, run=run)
    principal = Principal(7, "user", SOURCE_ASSERTION)
    scope = binding(principal, 42)
    selected.inputs.open({"id": "current", "scope": scope})
    cfg = {"configurable": {"thread_id": "conv_42", "principal": principal,
                            "user_id": 7, "conversation_id": 42}}

    def token(uid, role="user"):
        return jwt.encode({"sub": uid, "role": role, "aud": "agent",
                           "exp": int(time.time()) + 60}, settings.jwt_secret, algorithm="HS256")

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=application),
                                    base_url="http://fixture") as client:
            payload = {"user_id": 999, "conversation_id": 42, "task_id": "current",
                       "input_id": "first", "text": "只列标题"}
            with selected.conversation_guard(cfg):
                response = await asyncio.wait_for(client.post("/tasks/input", json=payload,
                    headers={"X-Agent-Assertion": token(7)}), timeout=2)
            assert response.status_code == 200
            assert response.json()["input"]["status"] == "queued"
            assert (await client.post("/tasks/input", json=payload)).status_code == 401
            for uid, role, cid in [(8, "user", 42), (7, "admin", 42), (7, "user", 43)]:
                rejected = await client.post("/tasks/input", json={**payload,"conversation_id":cid},
                    headers={"X-Agent-Assertion":token(uid, role)})
                assert rejected.status_code == 409
            status = await client.post("/tasks/inputs/status", json={"user_id":7,
                "conversation_id":42,"task_id":"current"},
                headers={"X-Agent-Assertion":token(7)})
            assert status.json()["inputs"] == [response.json()["input"]]

    asyncio.run(scenario())
