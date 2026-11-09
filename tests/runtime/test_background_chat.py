"""Background admission is idempotent, scoped, and independent of subscriptions."""

import asyncio
import inspect
import json
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from agent.api.routes.chat_runs import Submission
from agent.background import workflows
from agent.background.service import ChatRuns, RunConflict
from agent.principal import Principal

USER = Principal(7, "user", "assertion")


class Client:
    def __init__(self):
        self.rows = {}
        self.submitted = []
        self.cancelled = []

    def list_workflows(self, **query):
        return [row for key, row in self.rows.items()
                if row.authenticated_user == query["user"]
                and (not query.get("workflow_ids") or key in query["workflow_ids"])
                and (not query.get("status") or row.status in query["status"])
                and all(row.attributes.get(k) == v
                        for k, v in (query.get("attributes") or {}).items())]

    def enqueue(self, options, payload):
        self.submitted.append(payload)
        self.rows[options["workflow_id"]] = SimpleNamespace(
            workflow_id=options["workflow_id"], status="ENQUEUED", output=None,
            authenticated_user=options["authenticated_user"], attributes=options["attributes"])

    def cancel_workflow(self, identifier):
        self.cancelled.append(identifier)
        self.rows[identifier].status = "CANCELLED"


@pytest.fixture
def runs():
    @contextmanager
    def mutation(owner):
        assert owner > 0
        yield

    return ChatRuns(SimpleNamespace(client=Client(), mutation=mutation))


def submission(number=1, conversation=42, text="read only"):
    return Submission(run_id=f"{number:032x}", conversation_id=conversation,
        body=json.dumps({"message": text, "conversation_id": conversation}), ticket="opaque-ticket")


def test_repeated_submission_is_one_run_and_body_changes_are_rejected(runs):
    first = runs.create(USER, submission())
    assert runs.create(USER, submission()) == first
    assert len(runs.client.submitted) == 1
    with pytest.raises(RunConflict):
        runs.create(USER, submission(text="different request"))
    assert "ticket" not in json.dumps(runs.list(USER))


def test_another_conversation_can_start_while_same_conversation_is_busy(runs):
    runs.create(USER, submission())
    with pytest.raises(RunConflict, match="conversation"):
        runs.create(USER, submission(2))
    runs.create(USER, submission(2, 43))
    assert len(runs.client.submitted) == 2
    assert len(runs.list(USER, 42)["items"]) == 1


def test_owner_role_and_capacity_boundaries(runs):
    for n in range(4):
        runs.create(USER, submission(n + 1, n + 40))
    with pytest.raises(RunConflict, match="too many"):
        runs.create(USER, submission(5, 50))
    for principal in (Principal(8, "user", "assertion"), Principal(7, "admin", "assertion")):
        with pytest.raises(LookupError):
            runs.cancel(principal, submission().run_id)
    assert not runs.client.cancelled
    runs.cancel(USER, submission().run_id)
    runs.create(USER, submission(5, 50))
    assert len(runs.client.submitted) == 5


def test_recovered_attempt_does_not_call_host_or_publish_a_second_terminal(monkeypatch):
    monkeypatch.setattr(workflows.DBOS, "get_event_async", AsyncMock(return_value=True))
    monkeypatch.setattr(workflows.DBOS, "set_event_async", AsyncMock())
    published = AsyncMock()
    monkeypatch.setattr(workflows, "publish", published)
    result = asyncio.run(inspect.unwrap(workflows.perform)({"body": "{}", "ticket": "opaque"}))
    assert result["status"] == "interrupted" and result["terminal"]["event"] == "error"
    published.assert_not_called()
    workflows.DBOS.set_event_async.assert_not_called()


def test_worker_uses_one_frozen_request_and_commits_before_terminal_publication(monkeypatch):
    calls = []

    def handle(request):
        calls.append(request)
        assert request.url.path == "/api/chat/runs/execute"
        assert request.headers["X-Chat-Run-Ticket"] == "opaque"
        assert request.content == b'{"message":"hello"}'
        return httpx.Response(200, headers={"Content-Type": "text/event-stream"},
            text='event: text\ndata: "answer"\n\nevent: done\ndata: {}\n\n')

    client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
    monkeypatch.setattr(workflows.httpx, "AsyncClient", lambda **_: client)
    monkeypatch.setattr(workflows.DBOS, "get_event_async", AsyncMock(return_value=None))
    monkeypatch.setattr(workflows.DBOS, "set_event_async", AsyncMock())
    published = AsyncMock()
    monkeypatch.setattr(workflows, "publish", published)
    result = asyncio.run(inspect.unwrap(workflows.perform)(
        {"body": '{"message":"hello"}', "ticket": "opaque"}))
    assert len(calls) == 1 and result == {"status": "completed",
                                         "terminal": {"event": "done", "data": {}}}
    published.assert_awaited_once_with("text", "answer")


def test_closing_api_subscription_closes_reader_without_cancelling_execution(runs, monkeypatch):
    from agent.api.routes import chat_runs

    runs.create(USER, submission())
    closed = []

    async def stream(*args, **kwargs):
        assert kwargs["offset"] == 1
        try:
            yield {"event": "text", "data": "still running"}
            await asyncio.Event().wait()
        finally:
            closed.append(True)

    runs.client.read_stream_async = stream
    monkeypatch.setattr(chat_runs, "service", lambda _: (runs, USER))

    async def scenario():
        result = await chat_runs.events(None, submission().run_id, offset=1)
        item = await anext(result.body_iterator)
        assert item.startswith("id: 1\n") and "still running" in item
        await result.body_iterator.aclose()

    asyncio.run(scenario())
    assert closed == [True] and not runs.client.cancelled
    assert runs.owned(USER, submission().run_id).status == "ENQUEUED"


def test_chat_run_api_rejects_unsigned_and_cross_account_requests(runs, monkeypatch):
    import time

    import jwt

    from agent.main import create_app
    from config.settings import settings

    monkeypatch.setattr(settings, "agent_assertion_secret",
                        "synthetic-background-test-key-at-least-32-bytes")
    app = create_app()
    app.state.scheduling = SimpleNamespace(backend=runs.backend)

    def headers(uid):
        return {"X-Agent-Assertion": jwt.encode({"sub": uid, "role": "user", "aud": "agent",
            "exp": int(time.time()) + 60}, settings.agent_assertion_secret, algorithm="HS256")}

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://fixture") as client:
            unsigned = await client.post("/chat/runs", json=submission().model_dump())
            assert unsigned.status_code == 401
            created = await client.post("/chat/runs", json=submission().model_dump(),
                                        headers=headers(7))
            assert created.status_code == 202 and created.headers["cache-control"] == "no-store"
            other = await client.get("/chat/runs", headers=headers(8))
            assert other.json()["items"] == []
            denied = await client.post(f"/chat/runs/{submission().run_id}/cancel",
                                       headers=headers(8))
            assert denied.status_code == 404 and not runs.client.cancelled

    asyncio.run(scenario())
