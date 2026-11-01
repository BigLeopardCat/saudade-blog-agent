"""Schedule safety contracts at the tool, identity and time boundaries."""

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import httpx
import pytest
from pydantic import ValidationError

from agent import authz, confirm
from agent.domains.automations.preparation import tools
from agent.principal import Principal
from agent.scheduling.host import BlogHost
from agent.scheduling.models import Definition, parse_time
from agent.scheduling.readonly import ReadOnlyBlogApplication
from agent.scheduling.service import ScheduleService, run_view
from config.settings import settings

NOW = datetime(2026, 11, 2, tzinfo=timezone.utc)
ADMIN = Principal(7, "admin", "assertion")
IDENT = "sc_" + "a" * 32


def definition(**values):
    return Definition(
        title="提醒", action="reminder", instruction="喝水", delay_seconds=60, **values
    )


@pytest.mark.parametrize(
    "values",
    [
        {"run_at": NOW},
        {"delay_seconds": True},
        {"delay_seconds": 0},
        {"title": " "},
        {"instruction": "\x00"},
        {"timezone": "bogus"},
        {"cron": "* * * * *", "delay_seconds": None},
        {"cron": "0,2 * * * *", "delay_seconds": None},
        {"cron": "0 * * * * *", "delay_seconds": None},
        {"condition": {"article_id": 1}},
        {"condition": {"article_id": True}, "expires_at": NOW + timedelta(hours=1)},
    ],
)
def test_invalid_definitions_never_enter_the_queue(values):
    base = dict(title="提醒", action="reminder", instruction="喝水", delay_seconds=60)
    with pytest.raises(ValidationError):
        Definition(**{**base, **values})


def test_timezones_deadlines_and_relative_confirmation_time():
    assert parse_time("2026-11-02T08:00", "Asia/Shanghai") == NOW
    assert parse_time("2026-11-02T00:00Z", "Asia/Shanghai") == NOW
    for stamp in ("2026-11-01T01:30", "2026-03-08T02:30"):
        with pytest.raises(ValueError):
            parse_time(stamp, "America/New_York")
    plan = definition()
    assert plan.resolved_time(NOW) == NOW + timedelta(seconds=60)
    with pytest.raises(ValueError):
        definition(expires_at=NOW).validate_creation(NOW)
    for cron in ("*/5 * * * *", "30 8 * * 1-5", "0 0 1 * *"):
        Definition(title="循环", action="reminder", instruction="提醒", cron=cron)


@pytest.mark.parametrize(
    "principal",
    [
        Principal(7, "user", "assertion"),
        Principal(7, "admin", "body"),
        Principal(0, "admin", "assertion"),
    ],
)
def test_untrusted_or_nonadmin_cannot_contact_backend(principal):
    service = ScheduleService(None, lambda _: pytest.fail("must reject before host call"))
    with pytest.raises(PermissionError):
        service.get(principal, IDENT)


def test_fresh_role_and_owner_must_match():
    for fresh in (Principal(7, "user", "assertion"), Principal(8, "admin", "assertion")):
        service = ScheduleService(None, lambda _: fresh)
        with pytest.raises(PermissionError):
            service.authorize(ADMIN)


def test_real_preparation_signs_definition_without_creating_schedule(monkeypatch):
    monkeypatch.setattr(settings, "jwt_secret", "scheduling-fixture-secret-at-least-32")
    service = ScheduleService(None, lambda _: ADMIN, now=lambda: NOW)
    selected = tools(service, True)
    result = selected["prepare_automation"].invoke(
        {"title": "提醒", "instruction": "喝水", "action": "reminder", "delay_seconds": 60},
        config={
            "configurable": {
                "principal": ADMIN,
                "user_id": 7,
                "conversation_id": 42,
                "thread_id": "conv_42",
            }
        },
    )
    card = result.execution["pending_confirm"]
    grant = confirm.verify(card["token"], 7, 42)
    assert grant["executor"] == "automations"
    assert grant["specs"][0]["tool"] == "create_automation"
    args = grant["specs"][0]["args"]
    assert args["definition"]["delay_seconds"] == 60
    assert "不会携带当前聊天历史" in card["q"]
    assert card["token"] not in result.reply
    assert confirm.verify(card["token"], 8, 42) is None
    assert confirm.verify(card["token"], 7, 43) is None


def test_stale_authorization_is_a_typed_tool_failure():
    service = ScheduleService(None, lambda _: Principal(7, "user", "assertion"))
    result = tools(service, False)["list_automations"].invoke(
        {}, config={"configurable": {"principal": ADMIN}}
    )
    assert result.kind == "unavailable"


def test_readonly_background_agent_has_no_prepare_write_or_browser_capability():
    app = ReadOnlyBlogApplication()
    initial, domains = app.tools(
        {"configurable": {"principal": ADMIN, "user_id": 7}}, None, None, None, True
    )
    names = set(initial) | {name for group in domains.values() for name in group}
    assert "search_notes" in names
    assert "navigate_page" not in names
    assert "set_page_appearance" not in names
    assert "get_chat_history" not in names
    assert "devices" not in domains
    assert not any(name.startswith("prepare_") for name in names)
    for name in names:
        scope = authz.TOOL_SCOPE.get(name)
        assert scope not in {authz.SCOPE_WRITE_CONSOLE, authz.SCOPE_WRITE_OWN}
    assert app.session_key(ADMIN, 42) != app.session_key(Principal(8, "admin", "assertion"), 42)


def test_run_list_bounds_text_but_exact_result_retains_text():
    row = SimpleNamespace(
        workflow_id="run",
        status="SUCCESS",
        created_at=1,
        updated_at=2,
        output={"status": "completed", "text": "x" * 8000, "trace_id": "trace"},
        error=None,
    )
    preview = run_view(row)
    assert len(json.dumps(preview)) < 600
    assert run_view(row, full=True)["result"]["text"] == "x" * 8000


def test_host_rechecks_current_account_and_handles_hidden_article(monkeypatch):
    monkeypatch.setattr(settings, "jwt_secret", "scheduling-fixture-secret-at-least-32")
    requests = []

    def handler(request):
        requests.append(request)
        if request.url.path.endswith("automation-actor"):
            return httpx.Response(200, json={"code": 200, "data": {"id": 7, "role": "admin"}})
        return httpx.Response(404)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        host = BlogHost(client)
        assert host.actor(7) == ADMIN
        assert not host.article_public(7, 54)
        with pytest.raises(PermissionError):
            host.actor(8)
    assert requests[0].headers["authorization"].startswith("Bearer ")


def test_background_agent_uses_standard_runtime_and_reuses_finished_checkpoint(monkeypatch):
    from contextlib import ExitStack

    from langchain_core.messages import AIMessageChunk
    from langgraph.checkpoint.memory import InMemorySaver

    from agent.scheduling import readonly

    class Model:
        max_tokens = 64
        calls = 0

        def bind_tools(self, tools, **kwargs):
            self.menus = tools
            return self

        def stream(self, messages, **kwargs):
            self.calls += 1
            yield AIMessageChunk(content="隔离任务结果")

    model = Model()
    monkeypatch.setattr(readonly.checkpoint, "build_checkpointer", lambda *_, **__: InMemorySaver())
    monkeypatch.setattr(readonly, "get_llm", lambda **_: model)
    with ExitStack() as resources:
        runner = readonly.ReadOnlyRunner(resources)
        first = runner.run(ADMIN, "isolated-run", "分析文章")
        replay = runner.run(ADMIN, "isolated-run", "分析文章")
        runner.runtime.update_state(
            {
                "configurable": {
                    "thread_id": first["thread_id"],
                    "principal": ADMIN,
                    "user_id": ADMIN.uid,
                }
            },
            {"last_outcome": "blocked"},
        )
        blocked = runner.run(ADMIN, "isolated-run", "分析文章")
        assert blocked["status"] == "incomplete"
    assert first["text"] == replay["text"] == "隔离任务结果"
    assert replay["reused"] and model.calls == 1
    assert first["thread_id"].startswith("scheduled_")


def test_confirmed_create_uses_existing_execution_ledger_and_replay_does_not_reinvoke(
    monkeypatch, tmp_path
):
    from _framework_ledger import Ledger
    from _own_support import config

    from agent.domains.automations.transactions import BoundDomain
    from agent.runtime import writes

    monkeypatch.setattr(settings, "jwt_secret", "scheduling-fixture-secret-at-least-32")
    service = ScheduleService(None, lambda _: ADMIN, now=lambda: NOW)
    cfg = config(principal=ADMIN)
    prepared = tools(service, True)["prepare_automation"].invoke(
        {"title": "提醒", "instruction": "喝水", "action": "reminder", "delay_seconds": 60},
        config=cfg,
    )
    grant = confirm.verify(prepared.execution["pending_confirm"]["token"], 7, 42)
    calls = []

    def create(principal, identifier, definition):
        calls.append(identifier)
        return {"changed": True, "schedule": {"id": identifier, "definition": definition}}

    monkeypatch.setattr(service, "create", create)
    ledger = Ledger(tmp_path / "execution.sqlite")
    monkeypatch.setattr(writes, "ledger_for", lambda _: ledger)
    domain = BoundDomain(service)
    first = domain.execute(grant, cfg, object())
    replay = domain.execute(grant, cfg, object())
    assert first["receipts"] and not first["blocked"]
    assert replay["receipts"] and len(calls) == 1


def test_model_discovers_scheduling_and_prepares_card_through_real_middleware(monkeypatch):
    from _own_support import config
    from langchain_core.messages import AIMessage, HumanMessage
    from langgraph.checkpoint.memory import InMemorySaver

    from agent.products.blog.application import BlogApplication
    from agent.runtime.native import NativeRuntime
    from agent.runtime.native_store import NativeConversationStore

    monkeypatch.setattr(settings, "jwt_secret", "scheduling-fixture-secret-at-least-32")
    service = ScheduleService(None, lambda _: ADMIN, now=lambda: NOW)
    responses = iter(
        [
            AIMessage(
                content="",
                tool_calls=[
                    {"name": "load_admin_automations_info_tools", "args": {}, "id": "load"}
                ],
            ),
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "prepare_automation",
                        "args": {
                            "title": "提醒",
                            "instruction": "喝水",
                            "action": "reminder",
                            "delay_seconds": 60,
                        },
                        "id": "prepare",
                    }
                ],
            ),
        ]
    )

    class Model:
        max_tokens = 128

        def bind_tools(self, tools, **kwargs):
            return self

        def invoke(self, messages):
            return next(responses)

    runtime = NativeRuntime(
        NativeConversationStore(InMemorySaver()),
        application=BlogApplication(scheduling=service),
        model_factory=lambda **_: Model(),
    )
    result = list(
        runtime.stream_events(
            {"messages": [HumanMessage(content="一分钟后提醒我喝水")]}, config(principal=ADMIN)
        )
    )[-1]
    assert result.execution["pending_confirm"]["workflow"] == "automation_create"
    assert not result.execution.get("receipts")
