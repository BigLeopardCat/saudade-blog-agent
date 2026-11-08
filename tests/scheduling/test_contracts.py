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
from agent.scheduling.host import BlogHost, HostOutcomeUnverified
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


def test_optional_schedule_semantics_are_visible_on_the_actual_tool_properties():
    from langchain_core.utils.function_calling import convert_to_openai_tool

    from agent.domains.categories.preparation import prepare_category_create

    schema = convert_to_openai_tool(prepare_category_create)
    schedule = schema["function"]["parameters"]["properties"]["schedule"]
    properties = next(branch["properties"] for branch in schedule["anyOf"]
                      if "properties" in branch)
    assert "date/time" in properties["run_at"]["description"].lower()
    assert "after approval" in properties["delay_seconds"]["description"].lower()


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
    monkeypatch.setattr(settings, "confirmation_secret", "scheduling-fixture-secret-at-least-32")
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
    monkeypatch.setattr(settings, "confirmation_secret", "scheduling-fixture-secret-at-least-32")
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
                    "background_run_id": "isolated-run",
                }
            },
            {"last_outcome": "blocked"},
        )
        blocked = runner.run(ADMIN, "isolated-run", "分析文章")
        assert blocked["status"] == "incomplete"
    assert first["text"] == replay["text"] == "隔离任务结果"
    assert replay["reused"] and model.calls == 1
    assert first["thread_id"].startswith("background_")


def test_confirmed_create_uses_existing_execution_ledger_and_replay_does_not_reinvoke(
    monkeypatch, tmp_path
):
    from _framework_ledger import Ledger
    from _own_support import config

    from agent.domains.automations.transactions import BoundDomain
    from agent.runtime import writes

    monkeypatch.setattr(settings, "confirmation_secret", "scheduling-fixture-secret-at-least-32")
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
    from agent.runtime.checkpoint_conversations import CheckpointConversationStore
    from agent.runtime.service import AgentRuntime

    monkeypatch.setattr(settings, "confirmation_secret", "scheduling-fixture-secret-at-least-32")
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

    runtime = AgentRuntime(
        CheckpointConversationStore(InMemorySaver()),
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


def test_notification_preview_is_explicit_and_receipt_identity_is_verified(monkeypatch):
    monkeypatch.setattr(settings, "confirmation_secret", "scheduling-fixture-secret-at-least-32")
    receipt = {"notificationId": 42, "replayed": False}
    sent = []

    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, json={"code": 200, "data": {"id": 7, "role": "admin"}})
        sent.append(json.loads(request.content))
        return httpx.Response(200, json={"code": 200, "data": receipt})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        host = BlogHost(client)
        result = host.notify(7, "run", "提醒", "长" * 1100)
        assert result["delivered"] and result["notification_id"] == 42
        assert len(sent[0]["content"]) <= 1000 and "完整结果" in sent[0]["content"]
        receipt["notificationId"] = True
        with pytest.raises(ValueError):
            host.notify(7, "run", "提醒", "正文")


def test_schedule_action_text_uses_verified_title_and_covers_all_operations():
    from agent.action_text import receipt_action
    from agent.domains.automations.presentation import LABELS
    from agent.domains.automations.transactions import PERMISSION_OPERATIONS

    assert set(LABELS) == PERMISSION_OPERATIONS
    result = receipt_action(
        "create_automation",
        {"definition": {"title": "proposal"}},
        {"schedule": {"definition": {"title": "verified"}}},
    )
    assert result == "创建计划任务「verified」"
    assert all(receipt_action(name, {}, {}) for name in PERMISSION_OPERATIONS)


def test_background_task_identity_is_owner_bound_and_separate_from_chat():
    from agent.runtime.task_scope import background_thread, binding

    cfg = {
        "principal": ADMIN,
        "user_id": ADMIN.uid,
        "background_run_id": "run",
        "thread_id": background_thread(ADMIN.uid, "run"),
    }
    assert binding({"configurable": cfg}) == {
        "uid": ADMIN.uid,
        "role": "admin",
        "background_run_id": "run",
    }
    for mutation in (
        {"conversation_id": 42},
        {"user_id": 8},
        {"thread_id": background_thread(8, "run")},
        {"background_run_id": ""},
    ):
        with pytest.raises(ValueError):
            binding({"configurable": {**cfg, **mutation}})


def test_ui_list_uses_active_backend_filter_and_server_side_cron_time():
    from unittest.mock import Mock

    recurring = {'status': 'ACTIVE', 'context': {'schedule_id': IDENT,
        'definition': definition().model_copy(update={'delay_seconds': None,
            'cron': '0 * * * *'}).model_dump(mode='json')}}
    once = SimpleNamespace(status='SUCCESS', output={'status':'completed'},
        attributes={'schedule_id': 'sc_'+'b'*32,
        'definition': definition().model_dump(mode='json'), 'start_at': NOW.isoformat()})
    client = SimpleNamespace(list_workflows=Mock(return_value=[once]))
    backend = SimpleNamespace(client=client, recurring_schedules=lambda owner: [recurring])
    service = ScheduleService(backend, lambda uid: ADMIN, now=lambda: NOW)
    normal = service.list(ADMIN)
    assert 'instruction' not in normal['items'][0]
    result = service.list(ADMIN, 20, 0, True)
    assert len(result['items']) == 1 and result['items'][0]['status'] == 'active'
    item = result['items'][0]
    assert item['instruction'] == '喝水' and len(item['version']) == 64
    due = datetime.fromisoformat(item['next_run_at']).astimezone(timezone.utc)
    assert due == NOW + timedelta(hours=1)
    assert client.list_workflows.call_args.kwargs['attributes'] == {'cancelled': False}


def test_scheduled_publication_approval_freezes_exact_content_and_does_not_publish(monkeypatch):
    from _own_support import config

    monkeypatch.setattr(settings, "confirmation_secret", "scheduling-fixture-secret-at-least-32")
    service = ScheduleService(None, lambda _: ADMIN, now=lambda: NOW)
    result = tools(service, True)["prepare_automation"].invoke(
        {"title": "测试公告", "instruction": " 保留原文\n😀 ",
         "action": "publish_announcement", "delay_seconds": 300},
        config=config(principal=ADMIN),
    )
    card = result.execution["pending_confirm"]
    grant = confirm.verify(card["token"], ADMIN.uid, 42)
    frozen = grant["specs"][0]["args"]["definition"]
    assert frozen["action"] == "publish_announcement" and frozen["delay_seconds"] == 300
    assert frozen["title"] == "测试公告" and frozen["instruction"] == " 保留原文\n😀 "
    assert "发布时的全部用户" in card["q"] and "无需再次确认" in card["q"]
    assert not result.execution.get("receipts")


@pytest.mark.parametrize("title,content", [("bad\n", "body"), ("title", "bad\x01")])
def test_scheduled_publication_uses_same_content_validation_as_interactive_posts(title, content):
    with pytest.raises(ValidationError):
        Definition(title=title, instruction=content,
                   action="publish_announcement", delay_seconds=300)


def test_scheduled_publication_checks_bound_receipt_and_never_calls_model(monkeypatch):
    from agent.scheduling.models import fingerprint

    monkeypatch.setattr(settings, "confirmation_secret", "scheduling-fixture-secret-at-least-32")
    title, text = "测试公告", " 原文\n😀 "
    receipt = {"announcementId": 54, "notificationCount": 2,
               "payloadHash": fingerprint([title, text]), "replayed": False}
    sent = []

    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, json={"code": 200, "data": {"id": 7, "role": "admin"}})
        sent.append(json.loads(request.content))
        return httpx.Response(200, json={"code": 200, "data": receipt})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        host = BlogHost(client)
        result = host.publish_announcement(7, "run", title, text)
        assert result["status"] == "completed" and result["publication"]["announcementId"] == 54
        assert sent == [{"runId": "run", "title": title, "content": text}]
        receipt["payloadHash"] = "a" * 64
        with pytest.raises(HostOutcomeUnverified) as exc:
            host.publish_announcement(7, "run", title, text)
        assert exc.value.result()["reason"] == "execution_unverified"


def test_scheduled_publication_worker_rechecks_pause_and_executes_frozen_payload():
    from unittest.mock import Mock

    from agent.scheduling.worker import WorkerActions

    value = Definition(title="公告", action="publish_announcement",
                       instruction="正文", delay_seconds=300)
    row = SimpleNamespace(attributes={"paused": True, "cancelled": False})
    backend = SimpleNamespace(once=lambda *_: row)
    host = SimpleNamespace(actor=lambda _: ADMIN,
                           publish_announcement=Mock(return_value={"status": "completed"}))
    worker = WorkerActions(backend, host, None)
    payload = {"owner_id": 7, "schedule_id": IDENT, "definition": value.model_dump(mode="json"),
               "start_at": datetime.now(timezone.utc).isoformat()}
    assert worker.perform(payload, "run")["status"] == "blocked"
    host.publish_announcement.assert_not_called()
    row.attributes["paused"] = False
    assert worker.perform(payload, "run")["status"] == "completed"
    host.publish_announcement.assert_called_once_with(7, "run", "公告", "正文")
    assert worker.runner is None
