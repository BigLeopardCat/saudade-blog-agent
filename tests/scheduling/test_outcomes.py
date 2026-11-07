"""Business completion and notification delivery remain distinct from DBOS state."""

import asyncio
import inspect
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from dbos import error

from agent.principal import Principal
from agent.scheduling import workflows
from agent.scheduling.host import BlogHost, HostOutcomeUnverified
from agent.scheduling.models import Definition
from agent.scheduling.service import ScheduleConflict, ScheduleService, once_view, run_view
from agent.scheduling.worker import WorkerActions
from config.settings import settings

ADMIN = Principal(7, "admin", "assertion")
IDENT = "sc_" + "a" * 32


def row(outcome="completed", *, state="SUCCESS", notification=None):
    result = {"status": outcome}
    if notification is not None:
        result["notification"] = notification
    return SimpleNamespace(
        status=state, output=result,
        attributes={"schedule_id": IDENT, "start_at": "2026-11-07T00:00:00+00:00",
                    "cancelled": False, "paused": False,
                    "definition": Definition(title="测试", action="reminder", instruction="通知",
                                             delay_seconds=300).model_dump(mode="json")},
    )


@pytest.mark.parametrize("status", ["blocked", "expired", "incomplete", "failed", "unknown"])
def test_business_noncompletion_is_visible_even_when_dbos_succeeded(status):
    view = once_view(row(status))
    assert view["state"] == "success" and view["status"] == status
    assert view["result"]["status"] == status and view["needs_attention"]


def test_verified_success_and_failed_notification_are_separate_facts():
    assert not once_view(row())["needs_attention"]
    view = once_view(row(notification={"delivered": False, "reason": "delivery_failed"}))
    assert view["status"] == "finished" and view["result"]["status"] == "completed"
    assert view["needs_attention"]
    missing = row(state="ERROR")
    missing.output = None
    assert once_view(missing)["result"]["status"] == "unknown"
    missing.workflow_id, missing.created_at = "run", 1
    missing.updated_at, missing.error = 2, "error"
    assert run_view(missing)["result"]["status"] == "unknown"
    assert run_view(missing)["error"] == "execution_failed"


def test_dbos_timer_progress_does_not_invalidate_the_approved_control_version():
    task = row(state="ENQUEUED")
    task.output = None
    first = once_view(task)
    for state in ("DELAYED", "PENDING"):
        task.status = state
        task.attributes["runtime_note"] = "non-control metadata"
        current = once_view(task)
        assert current["state"] != first["state"]
        assert current["version"] == first["version"]
    task.status, task.output = "SUCCESS", {"status": "completed", "text": "verified"}
    completed = once_view(task)
    assert completed["result"] != first["result"]
    assert completed["version"] == first["version"]


@pytest.mark.parametrize("field", ["paused", "cancelled", "acknowledged", "definition", "start_at"])
def test_real_control_mutations_still_invalidate_stale_versions(field):
    task = row(state="PENDING")
    before = once_view(task)["version"]
    if field == "definition":
        task.attributes[field] = {**task.attributes[field], "instruction": "changed approval"}
    elif field == "start_at":
        task.attributes[field] = "2026-11-08T00:00:00+00:00"
    else:
        task.attributes[field] = True
    assert once_view(task)["version"] != before


def test_control_after_timer_transition_uses_same_version_but_stale_resume_is_rejected():
    task = row(state="ENQUEUED")
    task.output = None
    client = SimpleNamespace(update_workflow_attributes=Mock(
        side_effect=lambda _, attrs, **kw: task.attributes.update(attrs)))
    backend = SimpleNamespace(client=client, mutation=lambda _: nullcontext(),
                              once=lambda *_: task, recurring=lambda *_: None)
    service = ScheduleService(backend, lambda _: ADMIN)
    approved = service.get(ADMIN, IDENT)
    task.status = "PENDING"
    paused = service.control(ADMIN, IDENT, "pause", approved["version"])["schedule"]
    assert paused["status"] == "paused" and paused["version"] != approved["version"]
    with pytest.raises(ScheduleConflict):
        service.control(ADMIN, IDENT, "resume", approved["version"])
    task.status, task.output = "SUCCESS", {"status": "completed"}
    with pytest.raises(ScheduleConflict, match="already ended"):
        service.control(ADMIN, IDENT, "resume", paused["version"])


def test_active_view_adds_bounded_attention_without_loading_each_task():
    active = row(state="PENDING")
    active.attributes["schedule_id"] = "sc_" + "b" * 32
    active.output = None
    blocked = row("blocked")
    completed = row()
    completed.attributes["schedule_id"] = "sc_" + "c" * 32
    client = SimpleNamespace(list_workflows=Mock(side_effect=[[active], [blocked, completed]]))
    backend = SimpleNamespace(client=client, recurring_schedules=lambda _: [])
    service = ScheduleService(backend, lambda _: ADMIN)
    listing = service.list(ADMIN, 20, active_only=True)
    assert [item["status"] for item in listing["items"]] == ["scheduled"]
    assert listing["recent_results"][0]["result"] == {"status": "blocked"}
    assert client.list_workflows.call_count == 2
    terminal_query = client.list_workflows.call_args.kwargs
    assert terminal_query["load_output"] and terminal_query["limit"] == 20
    assert terminal_query["status"] == ["SUCCESS", "ERROR"]


def test_attention_results_never_consume_the_twenty_active_task_slots():
    active = [row(state="PENDING") for _ in range(20)]
    for number, task in enumerate(active):
        task.attributes["schedule_id"] = f"sc_{number:032x}"
        task.output = None
    client = SimpleNamespace(list_workflows=Mock(side_effect=[active, [row("expired")]]))
    service = ScheduleService(SimpleNamespace(client=client, recurring_schedules=lambda _: []),
                              lambda _: ADMIN)
    result = service.list(ADMIN, 20, active_only=True)
    assert len(result["items"]) == 20 and len(result["recent_results"]) == 1
    assert result["recent_results"][0]["status"] == "expired"


def test_missed_timer_produces_a_visible_expired_business_outcome(monkeypatch):
    task = row()
    monkeypatch.setattr(workflows, "clock", lambda: "2026-11-07T02:00:00+00:00")
    ready = AsyncMock()
    monkeypatch.setattr(workflows, "ready", ready)
    result = asyncio.run(inspect.unwrap(workflows.once)({
        "definition": task.attributes["definition"], "start_at": task.attributes["start_at"]}))
    assert result == {"status": "expired", "reason": "missed_execution_window"}
    ready.assert_not_awaited()
    task.output = result
    assert once_view(task)["needs_attention"] and once_view(task)["status"] == "expired"


def test_incomplete_model_result_remains_incomplete_after_notification(monkeypatch):
    monkeypatch.setattr(workflows, "ready", AsyncMock(return_value=True))
    monkeypatch.setattr(workflows, "perform", AsyncMock(return_value={
        "status": "incomplete", "text": "读取服务不可用，分析未完成。"}))
    monkeypatch.setattr(workflows, "notify", AsyncMock(return_value={"delivered": True}))
    result = asyncio.run(inspect.unwrap(workflows.execute)({}, "run"))
    assert result["status"] == "incomplete" and result["notification"]["delivered"]
    task = row()
    task.output = result
    assert once_view(task)["needs_attention"]


def test_terminal_attention_acknowledgement_does_not_cancel_or_erase_execution():
    failed = row("incomplete")
    client = SimpleNamespace(update_workflow_attributes=Mock(
        side_effect=lambda _, attrs, **kw: failed.attributes.update(attrs)), cancel_workflow=Mock())
    backend = SimpleNamespace(client=client, mutation=lambda _: nullcontext(),
                              once=lambda *_: failed, recurring=lambda *_: None)
    service = ScheduleService(backend, lambda _: ADMIN)
    before = service.get(ADMIN, IDENT)
    with pytest.raises(ScheduleConflict):
        service.control(ADMIN, IDENT, "resume", before["version"])
    response = service.control(ADMIN, IDENT, "cancel", before["version"])
    after = response["schedule"]
    assert response["changed"] and after["acknowledged"] and not after["needs_attention"]
    assert after["status"] == "incomplete" and after["state"] == "success"
    assert after["result"] == before["result"] and not failed.attributes["cancelled"]
    client.cancel_workflow.assert_not_called()
    assert client.update_workflow_attributes.call_args.args[1] == {"acknowledged": True}


def test_notification_failure_preserves_verified_effect_and_does_not_repeat_it(monkeypatch):
    result = {"status": "completed", "text": "verified effect", "change": {"id": 54}}
    perform = AsyncMock(return_value=result)
    notify = AsyncMock(side_effect=error.DBOSMaxStepRetriesExceeded(
        "notify", 3, [httpx.ReadTimeout("synthetic")]))
    monkeypatch.setattr(workflows, "ready", AsyncMock(return_value=True))
    monkeypatch.setattr(workflows, "perform", perform)
    monkeypatch.setattr(workflows, "notify", notify)
    returned = asyncio.run(inspect.unwrap(workflows.execute)({}, "run"))
    assert returned["status"] == "completed" and returned["change"] == {"id": 54}
    assert returned["notification"] == {"delivered": False, "reason": "delivery_failed"}
    perform.assert_awaited_once()
    notify.assert_awaited_once()


def test_native_cancellation_is_never_changed_to_notification_failure(monkeypatch):
    monkeypatch.setattr(workflows, "ready", AsyncMock(return_value=True))
    monkeypatch.setattr(workflows, "perform", AsyncMock(return_value={
        "status": "completed", "text": "done"}))
    monkeypatch.setattr(workflows, "notify", AsyncMock(
        side_effect=error.DBOSWorkflowCancelledError("run")))
    with pytest.raises(error.DBOSWorkflowCancelledError):
        asyncio.run(inspect.unwrap(workflows.execute)({}, "run"))


def test_unverified_business_write_reaches_user_notification_without_posting_it_again(monkeypatch):
    monkeypatch.setattr(settings, "confirmation_secret", "schedule-fixture-secret-at-least-32")
    sent = []

    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, json={"code": 200, "data": {"id": 7, "role": "admin"}})
        sent.append(request.url.path)
        if request.url.path.endswith("automation-announcements"):
            raise httpx.ReadTimeout("remote outcome uncertain", request=request)
        return httpx.Response(200, json={"code": 200, "data": {
            "notificationId": 10, "replayed": False}})

    definition = Definition(title="公告", action="publish_announcement", instruction="批准正文",
                            delay_seconds=300)
    payload = {"owner_id": 7, "definition": definition.model_dump(mode="json")}
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        worker = WorkerActions(None, BlogHost(client), None)
        worker.enabled = Mock(return_value=True)
        worker.deliverable = Mock(return_value=True)
        monkeypatch.setattr(workflows, "actions", lambda: worker)
        monkeypatch.setattr(workflows, "ready", AsyncMock(return_value=True))
        monkeypatch.setattr(workflows, "perform", inspect.unwrap(workflows.perform))
        monkeypatch.setattr(workflows, "notify", inspect.unwrap(workflows.notify))
        returned = asyncio.run(inspect.unwrap(workflows.execute)(payload, "run-bound"))
    assert returned["status"] == "incomplete" and returned["reason"] == "execution_unverified"
    assert returned["notification"]["delivered"]
    assert sent == ["/api/protected/automation-announcements", "/api/protected/automation-results"]


@pytest.mark.parametrize("action", ["change", "publish_announcement"])
@pytest.mark.parametrize("failure", ["timeout", "bad_json", "wrong_receipt", "503"])
def test_sent_write_without_verified_receipt_is_reported_without_replay(
    monkeypatch, action, failure
):
    monkeypatch.setattr(settings, "confirmation_secret", "schedule-fixture-secret-at-least-32")
    sent = []

    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, json={"code": 200, "data": {"id": 7, "role": "admin"}})
        sent.append(request)
        if failure == "timeout":
            raise httpx.ReadTimeout("remote may have committed", request=request)
        if failure == "bad_json":
            return httpx.Response(200, content=b"not json")
        if failure == "503":
            return httpx.Response(503)
        return httpx.Response(200, json={"code": 200, "data": {"payloadHash": "wrong"}})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        host = BlogHost(client)
        args = ((7, "run-bound", {}, "title") if action == "change"
                else (7, "run-bound", "title", "body"))
        with pytest.raises(HostOutcomeUnverified) as exc:
            getattr(host, action)(*args)
        result = exc.value.result()
    assert result["status"] == "incomplete" and result["reason"] == "execution_unverified"
    assert result["run_id"] == "run-bound" and "可能已执行" in result["text"]
    assert len(sent) == 1


@pytest.mark.parametrize("action", ["change", "publish_announcement"])
def test_revoked_role_blocks_before_any_write_request(monkeypatch, action):
    monkeypatch.setattr(settings, "confirmation_secret", "schedule-fixture-secret-at-least-32")
    sent = []

    def handler(request):
        sent.append(request.method)
        return httpx.Response(403)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        host = BlogHost(client)
        args = (7, "run", {}, "title") if action == "change" else (7, "run", "title", "body")
        assert getattr(host, action)(*args)["status"] == "blocked"
    assert sent == ["GET"]


def test_invalid_actor_evidence_is_never_labelled_an_attempted_business_effect():
    host = BlogHost(Mock())
    host.actor = Mock(side_effect=ValueError("malformed identity response"))
    with pytest.raises(ValueError, match="malformed identity"):
        host.publish_announcement(7, "run", "title", "body")
    host.client.post.assert_not_called()
