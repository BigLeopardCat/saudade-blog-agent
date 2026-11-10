"""Scheduled writes bind approved payloads and reject invalid receipts or stale authority."""

import hashlib
import json
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest
from pydantic import ValidationError

from agent.scheduling import models
from agent.scheduling.host import BlogHost, HostOutcomeUnverified
from agent.scheduling.models import Definition, Trigger
from agent.scheduling.worker import WorkerActions


def change():
    return {
        "kind": "categories",
        "change": {
            "version": "a" * 64,
            "intent": {"action": "create", "target": None, "fields": {"name": "密码学"}},
        },
    }


def test_local_clock_time_is_resolved_by_server_without_relative_seconds(monkeypatch):
    now = datetime(2026, 11, 5, 4, 16, 2, tzinfo=timezone.utc)
    monkeypatch.setattr(models, "utc_now", lambda: now)
    for stamp in ["15:00", "2026-11-05T15:00", "2026-11-05T15:00+08:00"]:
        trigger = Trigger(run_at=stamp)
        assert trigger.resolved_time(now) == datetime(2026, 11, 5, 7, tzinfo=timezone.utc)
        assert trigger.delay_seconds is None
    assert Trigger(delay_seconds=300).resolved_time(now).minute == 21


@pytest.mark.parametrize(
    "extra",
    [
        {"action": "reminder", "write": change()},
        {"write": {"kind": "sql", "change": {"query": "DROP TABLE note"}}},
        {
            "write": {
                "kind": "categories",
                "change": {
                    "version": "a" * 64,
                    "intent": {"action": "create", "fields": {"name": "x", "admin": True}},
                },
            }
        },
        {"cron": "0 8 * * *", "delay_seconds": None},
    ],
)
def test_invalid_or_recurring_version_bound_effects_cannot_be_enqueued(extra):
    with pytest.raises(ValidationError):
        Definition.model_validate(
            {
                "title": "分类",
                "instruction": "执行审批内容",
                "action": "admin_write",
                "write": change(),
                "delay_seconds": 300,
                **extra,
            }
        )


@pytest.mark.parametrize(
    "status,wrong_receipt", [(200, False), (200, True), (403, False), (409, False), (503, False)]
)
def test_effect_host_verifies_identity_and_payload_bound_backend_receipt(status, wrong_receipt):
    write = Definition(
        title="分类",
        instruction="审批内容",
        action="admin_write",
        write=change(),
        delay_seconds=300,
    ).write
    digest = hashlib.sha256(
        json.dumps(write, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    calls = []

    def handle(request):
        calls.append(request)
        if request.method == "GET":
            return httpx.Response(200, json={"code": 200, "data": {"id": 7, "role": "admin"}})
        assert json.loads(request.content) == {"runId": "run", "write": write}
        return httpx.Response(
            status,
            json={
                "code": 200,
                "data": {
                    "payloadHash": "b" * 64 if wrong_receipt else digest,
                    "replayed": False,
                    "outcome": {"changed": True},
                },
            },
        )

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        host = BlogHost(client)
        host.headers = lambda _: {}
        if wrong_receipt or status == 503:
            with pytest.raises(HostOutcomeUnverified) as exc:
                host.change(7, "run", write, "分类")
            result = exc.value.result()
            assert result["status"] == "incomplete"
            assert result["reason"] == "execution_unverified"
            assert result["run_id"] == "run"
        else:
            result = host.change(7, "run", write, "分类")
            assert result["status"] == {200: "completed", 403: "blocked", 409: "incomplete"}[status]
    assert len(calls) == 2


def test_paused_business_change_cannot_execute_but_same_payload_runs_once_when_enabled():
    definition = Definition(
        title="分类",
        instruction="审批内容",
        action="admin_write",
        write=change(),
        delay_seconds=300,
    )
    payload = {"owner_id": 7, "definition": definition.model_dump(mode="json")}
    host = SimpleNamespace(change=Mock(return_value={"status": "completed"}))
    worker = WorkerActions(None, host, None)
    worker.enabled = Mock(return_value=False)
    assert worker.perform(payload, "run")["status"] == "blocked"
    host.change.assert_not_called()
    worker.enabled.assert_called_once_with(payload, allow_paused=False)
    worker.enabled.return_value = True
    assert worker.perform(payload, "run")["status"] == "completed"
    host.change.assert_called_once_with(7, "run", definition.write, "分类")


def test_disabled_scheduler_never_issues_a_future_execution_card(monkeypatch):
    from agent import confirm
    from agent.principal import Principal
    from agent.scheduling.effects import propose
    from config.settings import settings

    monkeypatch.setattr(settings, "scheduling_enabled", False)
    signer = Mock(side_effect=AssertionError("must not issue an unavailable schedule"))
    monkeypatch.setattr(confirm, "sign", signer)
    result = propose(
        Principal(7, "admin", "assertion"),
        42,
        "categories",
        {},
        "批准内容",
        "分类",
        {"delay_seconds": 300},
    )
    assert result.kind == "unavailable"
    signer.assert_not_called()


def test_scheduled_change_exposes_policy_without_claiming_execution(monkeypatch):
    from agent.principal import Principal
    from agent.scheduling.effects import propose
    from config.settings import settings

    monkeypatch.setattr(settings, "scheduling_enabled", True)
    monkeypatch.setattr(settings, "confirmation_secret", "scheduling-fixture-secret-at-least-32")
    original = {"verified_target": "category fixture"}
    result = propose(
        Principal(7, "admin", "assertion"), 42, "categories",
        {"tool": "create_category", "args": change()["change"]},
        "创建分类？", "分类", {"delay_seconds": 300}, preparation_facts=original,
    )
    pending = result.execution["pending_confirm"]
    assert pending["preparation_facts"]["execution_policy"] == {
        "target_version_guard": True,
        "on_target_change": "stop_without_replanning",
        "result_notification": "best_effort",
    }
    assert pending["preparation_facts"]["verified_target"] == original["verified_target"]
    assert pending["preparation_facts"]["timing"]["basis"] == "after_approval"
    assert result.execution["receipts"] == []
    assert original == {"verified_target": "category fixture"}


def test_optional_write_payload_does_not_change_saved_read_schedule_identity():
    from contextlib import nullcontext
    from types import SimpleNamespace

    from agent.principal import Principal
    from agent.runtime.writes import operation_key
    from agent.scheduling.models import CreateArguments
    from agent.scheduling.service import ScheduleService

    owner = Principal(7, "admin", "assertion")
    identifier = "sc_" + "a" * 32
    definition = {
        "title": "reminder",
        "instruction": "read-only text",
        "action": "reminder",
        "timezone": "Asia/Shanghai",
        "run_at": None,
        "delay_seconds": 60,
        "cron": None,
        "expires_at": None,
        "condition": None,
        "grace_seconds": 3600,
    }
    frozen = {"schedule_id": identifier, "definition": definition}
    normalized = CreateArguments.model_validate(frozen).model_dump(mode="json")
    grant = {"uid": 7, "conv": 42, "jti": "b" * 32}
    assert operation_key(grant, "create_automation", normalized) == operation_key(
        grant, "create_automation", frozen
    )
    row = SimpleNamespace(
        status="DELAYED", output=None,
        attributes={
            "schedule_id": identifier,
            "definition": definition,
            "start_at": "2026-11-07T00:00:00+00:00",
        },
    )
    backend = SimpleNamespace(
        mutation=lambda _: nullcontext(),
        recurring=lambda *_: None,
        once=lambda *_: row,
        enqueue_once=Mock(),
    )
    service = ScheduleService(backend, lambda _: owner)
    result = service.create(owner, identifier, definition)
    assert result["changed"] is False
    assert result["schedule"]["definition"] == definition
    backend.enqueue_once.assert_not_called()


@pytest.mark.parametrize(
    "timing",
    [
        {"delay_seconds": 300},
        {"run_at": "2026-12-01T15:00", "timezone": "Asia/Shanghai"},
        {"delay_seconds": 300, "when_article_public": 54, "expires_at": "2026-12-02T15:00+08:00"},
    ],
)
def test_write_schedule_uses_the_shared_trigger_validation_and_storage_shape(timing):
    from agent.scheduling.models import WriteSchedule

    compact = WriteSchedule.model_validate(timing)
    full = Trigger.model_validate(compact.definition_values())
    assert full.delay_seconds == timing.get("delay_seconds")
    assert (full.condition.article_id if full.condition else None) == timing.get(
        "when_article_public"
    )
    definition = Definition(
        **compact.definition_values(),
        title="分类",
        instruction="已批准内容",
        action="admin_write",
        write=change(),
    )
    assert (
        definition.model_dump(mode="json")["condition"] == full.model_dump(mode="json")["condition"]
    )


@pytest.mark.parametrize(
    "timing",
    [
        {},
        {"delay_seconds": 300, "cron": "0 8 * * *"},
        {"delay_seconds": 300, "grace_seconds": 0},
        {"delay_seconds": 300, "run_at": "2026-12-01T15:00"},
        {"delay_seconds": 300, "when_article_public": 54},
        {"delay_seconds": 300, "timezone": "not/a-zone"},
    ],
)
def test_write_schedule_rejects_unsupported_or_inconsistent_controls(timing):
    from agent.scheduling.models import WriteSchedule

    with pytest.raises(ValidationError):
        WriteSchedule.model_validate(timing)
