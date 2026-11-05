"""Task input controls use signed identity without entering the held turn lock."""

import asyncio
import time
from types import SimpleNamespace

import httpx
import jwt
from langgraph.checkpoint.memory import InMemorySaver

from agent.main import create_app
from agent.principal import SOURCE_ASSERTION, Principal
from agent.products.blog.application import BlogApplication
from agent.runtime.checkpoint_conversations import CheckpointConversationStore
from agent.runtime.service import AgentRuntime
from agent.runtime.task_inputs import binding
from config.settings import settings


def test_signed_control_is_available_while_conversation_is_locked(monkeypatch):
    monkeypatch.setattr(settings, "jwt_secret", "task-input-test-secret-does-not-leave-the-test")
    monkeypatch.setattr(settings, "agent_require_assertion", True)
    application = create_app()
    selected = AgentRuntime(
        CheckpointConversationStore(InMemorySaver()), application=BlogApplication()
    )
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


def test_task_manager_lists_and_cancels_only_the_signed_owner(monkeypatch):
    from langchain_core.messages import HumanMessage

    monkeypatch.setattr(settings, 'jwt_secret', 'task-manager-api-fixture-with-32-characters')
    monkeypatch.setattr(settings, 'agent_require_assertion', True)
    application = create_app()
    selected = AgentRuntime(CheckpointConversationStore(InMemorySaver()),
                            application=BlogApplication())
    application.state.runtime = selected

    async def run(function, *args):
        return await asyncio.to_thread(function, *args)

    application.state.workers = SimpleNamespace(closing=False, run=run)
    principal = Principal(7, 'user', SOURCE_ASSERTION)
    task = {'id': 'paused', 'scope': binding(principal, 42),
            'input': [HumanMessage(content='待继续的任务')]}
    selected.inputs.open(task)
    selected.inputs.finish(task, [], paused=True)
    cfg = {'configurable': {'thread_id': 'conv_42'}}
    selected.update_state(cfg, {'run': task})

    def token(uid):
        return {'X-Agent-Assertion': jwt.encode({'sub': uid, 'role': 'user', 'aud': 'agent',
                    'exp': int(time.time()) + 60}, settings.jwt_secret, algorithm='HS256')}

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=application),
                                     base_url='http://fixture') as client:
            response = await client.get('/tasks/paused?user_id=999', headers=token(7))
            assert response.headers['Cache-Control'] == 'no-store'
            assert [r['task_id'] for r in response.json()['items']] == ['paused']
            foreign = await client.get('/tasks/paused?user_id=7', headers=token(8))
            assert foreign.json()['items'] == []
            assert (await client.get('/tasks/paused?user_id=7')).status_code == 401
            payload = {'user_id': 999, 'conversation_id': 42, 'task_id': 'paused'}
            rejected = await client.post('/tasks/cancel', json=payload, headers=token(8))
            assert rejected.status_code == 409
            response = await client.post('/tasks/cancel', json=payload, headers=token(7))
            assert response.status_code == 200 and response.json()['cancelled']
            assert selected.store.load(cfg).run is None

    asyncio.run(scenario())
