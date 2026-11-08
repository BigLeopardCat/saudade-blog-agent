"""Model-visible facts preserve source scope, unknown dimensions and approval timing."""

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import httpx
import pytest
from _framework_fixture import call, model_replies
from _own_support import config
from langgraph.checkpoint.memory import InMemorySaver
from test_public_utilities import snapshot

from agent import confirm
from agent.api.context import build_messages
from agent.api.schemas import ChatRequest
from agent.domains.automations.preparation import tools
from agent.domains.public_activity import list_guestbook
from agent.principal import Principal
from agent.products.blog.application import BlogApplication
from agent.runtime.checkpoint_conversations import CheckpointConversationStore
from agent.runtime.service import AgentRuntime
from agent.scheduling.effects import propose
from agent.scheduling.service import ScheduleService
from config.settings import settings
from tools import base

ADMIN = Principal(7, "admin", "assertion")
NOW = datetime(2026, 11, 7, 4, tzinfo=timezone.utc)


def selected_runtime(model, *, catalog=None, scheduling=None):
    return AgentRuntime(
        CheckpointConversationStore(InMemorySaver()),
        application=BlogApplication(catalog, scheduling=scheduling),
        model_factory=lambda **_: model,
    )


def tool_content(request, name):
    calls = {
        call["id"]: call["function"]["name"]
        for message in request["messages"]
        for call in message.get("tool_calls", [])
    }
    return json.loads(
        next(
            message["content"]
            for message in request["messages"]
            if message["role"] == "tool" and calls.get(message["tool_call_id"]) == name
        )
    )


def test_missing_weather_is_explicitly_unknown_while_its_historical_source_stays_unchanged():
    with model_replies({"content": "这是IP城市的天气。"}, {"content": "当前没有天气数据。"}) as (
        model,
        wire,
    ):
        runtime = selected_runtime(model)
        for weather in [snapshot(), None]:
            incoming = build_messages(
                ChatRequest(message="猫咪心情如何？", current_weather=weather), holds_context=True
            )
            list(runtime.stream_events({"messages": incoming}, config()))
        first, second = [request["messages"] for request in wire]
        assert first[0] == second[0]
        assert '"scope":"current_caller"' in str(first[0]["content"])
        assert '"status":"observed"' in str(first)
        assert '"location_kind":"ip_estimate"' in str(first)
        assert '"current_weather":{"status":"unknown"}' in str(second)
        assert "杭州" not in second[-1]["content"] and "Open-Meteo" not in second[-1]["content"]
        assert second[:len(first)] == first
        assert "杭州" in runtime.store.load(config()).messages[0].content
        assert len(wire) == 2


def test_framework_reads_page_sample_with_actual_dates_and_no_time_filter(monkeypatch):
    requests = []

    def handle(request):
        requests.append(request)
        assert dict(request.url.params) == {"page": "1", "page_size": "2"}
        rows = [
            {
                "id": ident,
                "source": "board",
                "approved": 1,
                "displayName": "",
                "displayNameTruncated": False,
                "title": "留言",
                "titleTruncated": False,
                "content": "你好",
                "contentTruncated": False,
                "createdAt": stamp,
            }
            for ident, stamp in [(9, "2026-10-28T12:00:00"), (8, "2026-09-23T12:00:00")]
        ]
        return httpx.Response(200, json={"code": 200, "data": rows, "total": 28})

    catalog = SimpleNamespace(
        initial_tools=lambda _: {"list_guestbook": list_guestbook}, domains=lambda *_: {}
    )
    with (
        httpx.Client(transport=httpx.MockTransport(handle)) as client,
        model_replies(
            {"tool_calls": [call("list_guestbook", limit=2)]},
            {"content": "已读的两条留言都在打招呼，还有其他留言未读。"},
        ) as (model, wire),
    ):
        monkeypatch.setattr(base, "_client", client)
        result = list(
            selected_runtime(model, catalog=catalog).stream_events(
                {
                    "messages": build_messages(
                        ChatRequest(message="最近留言聊什么？"), holds_context=True
                    )
                },
                config(),
            )
        )[-1]
        data = tool_content(wire[1], "list_guestbook")
        assert {key: data[key] for key in ["page", "pageSize", "total", "hasMore"]} == {
            "page": 1,
            "pageSize": 2,
            "total": 28,
            "hasMore": True,
        }
        assert data["filters"] == {"id": None}
        assert [row["createdAt"] for row in data["items"]] == [
            "2026-10-28T12:00:00",
            "2026-09-23T12:00:00",
        ]
        assert len(requests) == 1 and len(wire) == 2 and not result.execution


@pytest.mark.parametrize(
    "timing,basis",
    [
        ({"delay_seconds": 300}, "after_approval"),
        ({"run_at": "2026-11-08T15:00:00+08:00"}, "absolute"),
        ({"cron": "0 8 * * *"}, "cron"),
    ],
    ids=["relative-after-approval", "absolute-zone", "cron-no-next-run"],
)
def test_schedule_model_facts_exclude_credentials_and_do_not_enqueue(monkeypatch, timing, basis):
    monkeypatch.setattr(settings, "confirmation_secret", "timing-fixture-secret-at-least-32")
    service = ScheduleService(None, lambda _: ADMIN, now=lambda: NOW)
    prepare = tools(service, True)["prepare_automation"]
    catalog = SimpleNamespace(
        initial_tools=lambda _: {"prepare_automation": prepare}, domains=lambda *_: {}
    )
    with model_replies(
        {
            "tool_calls": [
                call(
                    "prepare_automation",
                    title="提醒",
                    instruction="喝水",
                    action="reminder",
                    **timing,
                )
            ]
        },
        {"content": "计划已准备，等待你确认。"},
    ) as (model, wire):
        result = list(
            selected_runtime(model, catalog=catalog).stream_events(
                {"messages": build_messages(ChatRequest(message="准备计划"), holds_context=True)},
                config(principal=ADMIN),
            )
        )[-1]
        card = result.execution["pending_confirm"]
        visible = tool_content(wire[1], "prepare_automation")
        assert visible["executed"] is False and visible["status"] == "awaiting_confirmation"
        fact = visible["preparation_facts"]["timing"]
        assert fact["basis"] == basis and fact["timezone"] == "Asia/Shanghai"
        assert fact["delay_seconds"] == timing.get("delay_seconds")
        assert fact["cron"] == timing.get("cron")
        assert "next_run_at" not in fact and card["token"] not in str(wire)
        assert "specs" not in visible and "schedule_id" not in str(visible)
        grant = confirm.verify(card["token"], 7, 42)
        definition = grant["specs"][0]["args"]["definition"]
        assert definition["delay_seconds"] == timing.get("delay_seconds")
        if basis == "after_approval":
            from agent.scheduling.models import Definition

            plan = Definition.model_validate(definition)
            approval = NOW + timedelta(hours=1)
            assert plan.resolved_time(approval) == approval + timedelta(seconds=300)
            assert fact["run_at"] is None
        elif basis == "absolute":
            assert datetime.fromisoformat(fact["run_at"]) == datetime.fromisoformat(
                timing["run_at"]
            )
            assert fact["run_at"].endswith("+08:00")
        else:
            assert fact["run_at"] is None
        assert len(wire) == 2 and not result.execution["receipts"]


def test_scheduled_business_change_uses_the_same_approval_timing_facts(monkeypatch):
    monkeypatch.setattr(settings, "scheduling_enabled", True)
    monkeypatch.setattr(settings, "confirmation_secret", "timing-fixture-secret-at-least-32")
    result = propose(
        ADMIN,
        42,
        "categories",
        {
            "tool": "create_category",
            "args": {
                "version": "a" * 64,
                "intent": {"action": "create", "target": None, "fields": {"name": "密码学"}},
            },
        },
        "新建分类",
        "密码学",
        {"delay_seconds": 300},
    )
    assert result.execution["pending_confirm"]["preparation_facts"]["timing"] == {
        "basis": "after_approval",
        "delay_seconds": 300,
        "run_at": None,
        "cron": None,
        "timezone": "Asia/Shanghai",
    }
    assert not result.execution["receipts"]
