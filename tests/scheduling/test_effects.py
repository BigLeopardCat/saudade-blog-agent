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
from agent.scheduling.host import BlogHost
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
        if wrong_receipt:
            with pytest.raises(ValueError):
                host.change(7, "run", write, "分类")
        elif status == 503:
            with pytest.raises(httpx.HTTPStatusError):
                host.change(7, "run", write, "分类")
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
