"""Announcement changes bind exact targets, fields and scope to native approvals."""

import copy
import hashlib
import json
import secrets

import httpx
import pytest
from _own_support import config
from pydantic import ValidationError

from agent import confirm
from agent.contracts.events import StreamEvent
from agent.domains.announcements import preparation, transactions
from agent.principal import SOURCE_ASSERTION, Principal
from agent.products.blog.confirmation import public_card
from agent.products.blog.pending import PendingOperation
from agent.runtime import writes
from eval.fixtures.ledger import Ledger
from tools import base


def admin_config(**values):
    return config(principal=Principal(uid=7, role="admin", source=SOURCE_ASSERTION), **values)


@pytest.fixture
def announcements(monkeypatch):
    state = {
        "rows": {
            11: {
                "id": 11,
                "title": "测试",
                "content": "原文 保持\n换行",
            }
        },
        "scope": [(101, 7), (102, 8)],
        "writes": 0,
        "requests": [],
        "revision": 0,
        "operations": {},
    }

    def plan(intent):
        before = None
        if intent["target"] is not None:
            target = intent["target"]
            found = [
                row
                for row in state["rows"].values()
                if row["id"] == target["id"] or row["title"] == target["title"]
            ]
            assert len(found) == 1
            before = copy.deepcopy(found[0])
        after = copy.deepcopy(before) if before else dict.fromkeys(("id", "title", "content"))
        if intent["action"] == "delete":
            after = None
        else:
            after.update(
                {key: value for key, value in intent["fields"].items() if value is not None}
            )
        scope = state["scope"]
        version = hashlib.sha256(
            json.dumps([intent, before, after, scope, state["revision"]], sort_keys=True).encode()
        ).hexdigest()
        return {
            "version": version,
            "before": before,
            "after": after,
            "notificationCount": len(scope),
        }

    def handle(request):
        state["requests"].append(request)
        if request.method == "GET" and "/announcement-operations/" in request.url.path:
            if state.get("lookup_missing"):
                return httpx.Response(404, json={"code": 404, "message": "not found"})
            if state.get("lookup_unavailable"):
                raise httpx.ReadTimeout("receipt query unavailable")
            identifier = request.url.path.rsplit("/", 1)[1]
            value = ({"status": "completed", "receipt": state["operations"][identifier]}
                     if identifier in state["operations"] else
                     {"status": "absent", "operationId": identifier})
            return httpx.Response(200, json={"code": 200, "data": value})
        payload = json.loads(request.content)
        if request.url.path.endswith("/prepare"):
            prepared = plan(payload)
            if state.get("wrong_plan"):
                prepared["after"]["title"] = "unrequested"
            return httpx.Response(200, json={"code": 200, "data": prepared})
        identifier = payload.get("operationId")
        if identifier:
            payload = payload["change"]
            if identifier in state["operations"]:
                return httpx.Response(200, json={"code": 200,
                                                "data": state["operations"][identifier]})
        if state.get("race"):
            state["revision"] += 1
        current = plan(payload["intent"])
        if current["version"] != payload["version"]:
            return httpx.Response(200, json={"code": 409, "message": "stale", "data": {}})
        state["writes"] += 1
        if current["after"] is not None:
            current["after"]["id"] = current["after"]["id"] or 12
            state["rows"][current["after"]["id"]] = copy.deepcopy(current["after"])
        else:
            state["rows"].pop(current["before"]["id"])
        result = {
            "changed": current["before"] != current["after"],
            "beforeVersion": payload["version"],
            "state": current,
        }
        if state.get("wrong_result"):
            result["state"]["notificationCount"] += 1
        if identifier:
            from agent.domains.announcements.recovery import fingerprint

            result = {"operationId": identifier, "requestHash": fingerprint(payload),
                      "outcome": result}
            state["operations"][identifier] = copy.deepcopy(result)
        if state.get("lost"):
            raise httpx.ReadTimeout("response lost after commit")
        return httpx.Response(200, json={"code": 200, "data": result})

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        monkeypatch.setattr(base, "_client", client)
        monkeypatch.setattr(confirm.settings, "confirmation_secret", secrets.token_urlsafe(32))
        yield state


def grant(prepared):
    return confirm.verify(prepared.execution["pending_confirm"]["token"], 7, 42)


def prepare(action, **args):
    return getattr(preparation, "prepare_announcement_" + action).invoke(
        args, config=admin_config()
    )


@pytest.mark.parametrize(
    "action,args,tool",
    [
        ("create", {"title": "新公告", "content": " 保持原文\n"}, "create_announcement"),
        (
            "update",
            {"announcement_title": "测试", "new_title": "演示", "content": "新的正文"},
            "update_announcement",
        ),
        ("delete", {"announcement_id": 11}, "delete_announcement"),
    ],
)
def test_prepare_is_read_only_and_confirmed_replay_is_one_write(
    announcements, tmp_path, action, args, tool
):
    prepared = prepare(action, **args)
    signed = grant(prepared)
    assert signed["executor"] == "announcements" and signed["specs"][0]["tool"] == tool
    assert set(signed["specs"][0]["args"]) == {"version", "intent"}
    assert not announcements["writes"]
    card = public_card(prepared.execution)
    fields = {row["label"]: row["value"] for row in card["details"]["fields"]}
    assert fields["关联个人通知"] == "2 条"
    if action == "update":
        sections = card["details"]["sections"]
        assert sections == [{"label": "原正文", "text": "原文 保持\n换行", "format": "markdown"},
                            {"label": "新正文", "text": "新的正文", "format": "markdown"}]
    if action == "delete":
        assert fields["公告编号"] == "11"
        assert any("删除公告及其关联个人通知" in row["text"] for row in card["notices"])
    ledger = Ledger(str(tmp_path / "once.sqlite3"))
    for _ in range(2):
        result = transactions.execute(signed, admin_config(), None, ledger=ledger)
        assert len(result["receipts"]) == 1 and not result["blocked"]
    assert announcements["writes"] == 1
    if action == "create":
        assert announcements["rows"][12]["content"] == " 保持原文\n"
    elif action == "update":
        assert announcements["rows"][11]["content"] == "新的正文"
    else:
        assert 11 not in announcements["rows"]


def test_noop_does_not_create_a_card(announcements):
    result = prepare("update", announcement_id=11, content="原文 保持\n换行")
    assert result.execution["noop_note"] and not result.execution.get("pending_confirm")
    assert not announcements["writes"]


@pytest.mark.parametrize("mode", ["scope", "row", "race"])
def test_changed_announcement_or_scope_never_executes_a_stale_approval(
    announcements, tmp_path, mode
):
    signed = grant(prepare("delete", announcement_title="测试"))
    if mode == "scope":
        announcements["scope"][0] = (103, None)
    elif mode == "row":
        announcements["rows"][11]["content"] = "changed without timestamp"
    else:
        announcements["race"] = True
    result = transactions.execute(
        signed, admin_config(), None, ledger=Ledger(str(tmp_path / "stale.sqlite3"))
    )
    assert not result["receipts"] and not announcements["writes"]


@pytest.mark.parametrize("mode", ["lost", "wrong_result"])
def test_lost_or_mismatched_result_is_not_repeated(announcements, tmp_path, mode):
    signed = grant(prepare("update", announcement_id=11, content="新的正文"))
    announcements[mode] = True
    ledger = Ledger(str(tmp_path / "unknown.sqlite3"))
    first = transactions.execute(signed, admin_config(), None, ledger=ledger)
    assert bool(first["receipts"]) is (mode == "lost")
    recovered = transactions.execute(signed, admin_config(), None, ledger=ledger)
    assert bool(recovered["receipts"]) is (mode == "lost")
    assert announcements["writes"] == 1 and announcements["rows"][11]["content"] == "新的正文"


def test_lost_response_and_unavailable_lookup_recover_after_restart(announcements, tmp_path):
    from contextlib import closing

    signed = grant(prepare("create", title="新公告", content="原文"))
    announcements.update(lost=True, lookup_unavailable=True)
    path = str(tmp_path / "recover.sqlite3")
    with closing(Ledger(path)) as ledger:
        first = transactions.execute(signed, admin_config(), None, ledger=ledger)
        assert not first["receipts"] and first["operation_outcome"]["operation_id"]
    announcements["lookup_unavailable"] = False
    with closing(Ledger(path)) as ledger:
        recovered = transactions.execute(signed, admin_config(), None, ledger=ledger)
    assert recovered["receipts"] and announcements["writes"] == 1
    posts = [r for r in announcements["requests"] if r.url.path.endswith("announcement-operations")]
    assert len(posts) == 1


@pytest.mark.parametrize("replay_only", [False, True])
def test_crash_before_keyed_request_can_only_retry_with_valid_approval(
    announcements, tmp_path, replay_only
):
    signed = grant(prepare("create", title="新公告", content="原文"))
    spec = signed["specs"][0]
    key = writes.operation_key(signed, spec["tool"], spec["args"])
    ledger = Ledger(str(tmp_path / "before-http.sqlite3"))
    ledger.claim(key, spec["tool"], signed["jti"], pending=True)
    assert ledger.dispatch(key, recoverable=True)
    cfg = admin_config(confirmation_replay_only=replay_only)
    result = transactions.execute(signed, cfg, None, ledger=ledger)
    assert bool(result["receipts"]) is not replay_only
    assert announcements["writes"] == (0 if replay_only else 1)
    posts = [r for r in announcements["requests"] if r.url.path.endswith("announcement-operations")]
    if posts:
        assert json.loads(posts[0].content)["operationId"] == key


@pytest.mark.parametrize("field", ["operationId", "requestHash"])
def test_recovery_rejects_other_operations_and_payloads(announcements, tmp_path, field):
    signed = grant(prepare("update", announcement_id=11, content="新的正文"))
    announcements.update(lost=True, lookup_unavailable=True)
    ledger = Ledger(str(tmp_path / "bound.sqlite3"))
    assert not transactions.execute(signed, admin_config(), None, ledger=ledger)["receipts"]
    announcements["lookup_unavailable"] = False
    saved = next(iter(announcements["operations"].values()))
    saved[field] = "0" * 64
    assert not transactions.execute(signed, admin_config(), None, ledger=ledger)["receipts"]
    assert announcements["writes"] == 1


def test_operation_lookup_is_read_only_and_available_only_to_admins(announcements, tmp_path):
    from agent.domains.announcements.recovery import get_announcement_operation

    signed = grant(prepare("update", announcement_id=11, content="新的正文"))
    ledger = Ledger(str(tmp_path / "query.sqlite3"))
    transactions.execute(signed, admin_config(), None, ledger=ledger)
    identifier = next(iter(announcements["operations"]))
    args = {"operation_id": identifier}
    count = len(announcements["requests"])
    assert get_announcement_operation.invoke(args, config=config()).kind == "unavailable"
    assert len(announcements["requests"]) == count
    result = get_announcement_operation.invoke(args, config=admin_config())
    assert result.data["status"] == "completed" and announcements["writes"] == 1
    absent = get_announcement_operation.invoke({"operation_id": "0" * 64}, config=admin_config())
    assert absent.data["status"] == "absent" and announcements["writes"] == 1


def test_operation_receipt_is_discovered_and_read_through_product_middleware(
    announcements, tmp_path, monkeypatch,
):
    from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

    from agent.products.blog.capabilities import ToolCatalog
    from agent.products.blog.tool_policy import BlogToolMiddleware
    from agent.runtime.session import ToolSession

    signed = grant(prepare("create", title="新公告", content="原文"))
    transactions.execute(signed, admin_config(), None,
                         ledger=Ledger(str(tmp_path / "tool.sqlite3")))
    identifier = next(iter(announcements["operations"]))
    announcements["requests"].clear()
    prompts = []

    class Model:
        def __init__(self):
            self.messages = iter([
                AIMessage(content="", tool_calls=[{"id": "load", "args": {},
                    "name": "load_admin_announcements_info_tools"}]),
                AIMessage(content="", tool_calls=[{"id": "read",
                    "args": {"operation_id": identifier}, "name": "get_announcement_operation"}]),
                AIMessage(content="已核对历史回执。"),
            ])

        def bind_tools(self, schemas, **kwargs):
            return self

        def invoke(self, messages):
            prompts.append(messages)
            return next(self.messages)

    cfg = admin_config()
    domain = ToolCatalog().domains(cfg, False)["admin_announcements"]
    assert set(domain) == {"get_announcement_operation"}
    result = ToolSession(Model(), cfg, {}, {"admin_announcements": domain},
        middleware=(BlogToolMiddleware,)).run([HumanMessage(content="核对刚才公告操作的历史回执")])
    message = next(row for row in prompts[-1] if isinstance(row, ToolMessage)
                   and row.name == "get_announcement_operation")
    assert result.reply == "已核对历史回执。"
    assert json.loads(message.content)["receipt"]["operationId"] == identifier
    assert len(announcements["requests"]) == 1
    assert announcements["requests"][0].method == "GET" and announcements["writes"] == 1


def test_missing_host_endpoint_never_authorizes_a_write_retry(announcements, tmp_path):
    signed = grant(prepare("create", title="新公告", content="原文"))
    spec = signed["specs"][0]
    key = writes.operation_key(signed, spec["tool"], spec["args"])
    ledger = Ledger(str(tmp_path / "old-host.sqlite3"))
    ledger.claim(key, spec["tool"], signed["jti"], pending=True)
    ledger.dispatch(key, recoverable=True)
    announcements["lookup_missing"] = True
    assert not transactions.execute(signed, admin_config(), None, ledger=ledger)["receipts"]
    assert announcements["writes"] == 0
    assert not any(r.url.path.endswith("/announcement-actions") for r in announcements["requests"])


def test_fingerprint_matches_host_unicode_vector():
    from agent.domains.announcements.recovery import fingerprint

    assert fingerprint({"version": "a" * 64, "intent": {"action": "create", "fields": {
        "title": "原文😀", "content": " 保留\n正文 "}}}) == (
            "50e3522ecbed43bdf7cff76b32fe7fbee5e9f6c1385f403060545539b38ccbe3")


def test_cancelled_card_is_durable_and_cannot_write(announcements, tmp_path, monkeypatch):
    prepared = prepare("delete", announcement_id=11)
    signed = grant(prepared)
    store = Ledger(str(tmp_path / "revoked.sqlite3"))
    monkeypatch.setattr(writes, "ledger_for", lambda saver: store)
    pending = PendingOperation(signed, prepared.execution["pending_action"])
    assert pending.cancel(admin_config(), None).execution["pending_event"]["event"] == "cancelled"
    assert not transactions.execute(signed, admin_config(), None, ledger=store)["receipts"]
    assert not announcements["writes"]


@pytest.mark.parametrize(
    "args", [{}, {"announcement_id": 11, "announcement_title": " "}, {"announcement_title": " "}]
)
def test_invalid_target_is_rejected_before_io(announcements, args):
    result = prepare("delete", **args)
    assert result.kind == "invalid_args" and not announcements["requests"]


def test_target_pair_is_cross_checked_before_an_announcement_card(announcements):
    assert prepare("delete", announcement_id=11, announcement_title="测试").execution[
        "pending_confirm"
    ]
    assert prepare("delete", announcement_id=11, announcement_title="不匹配").kind == "unavailable"
    assert not announcements["writes"]


def test_unrequested_plan_change_cannot_issue_a_card(announcements):
    announcements["wrong_plan"] = True
    assert prepare("update", announcement_id=11, content="新的正文").kind == "unavailable"
    assert not announcements["writes"]


@pytest.mark.parametrize(
    "principal",
    [Principal(uid=7, role="admin"), Principal(uid=7, role="user", source=SOURCE_ASSERTION)],
)
def test_unverified_or_unauthorized_actor_cannot_prepare(announcements, principal):
    result = preparation.prepare_announcement_delete.invoke(
        {"announcement_id": 11}, config=config(principal=principal)
    )
    assert result.kind == "unavailable" and not announcements["requests"]


def test_permission_revocation_and_malformed_specs_cannot_execute(announcements, tmp_path):
    signed = grant(prepare("delete", announcement_id=11))
    revoked = config(principal=Principal(uid=7, role="user", source=SOURCE_ASSERTION))
    ledger = Ledger(str(tmp_path / "denied.sqlite3"))
    assert not transactions.execute(signed, revoked, None, ledger=ledger)["receipts"]
    for specs in [
        [None],
        [
            {
                "tool": "delete_announcement",
                "args": {
                    "version": "a" * 64,
                    "intent": {"action": "delete", "target": {"id": True}},
                },
            }
        ],
    ]:
        with pytest.raises((ValueError, ValidationError)):
            transactions.validate({**signed, "specs": specs}, admin_config())
    assert not announcements["writes"]


def test_maximum_create_keeps_confirmation_token_bounded(announcements):
    result = prepare(
        "create",
        title="标" * 80,
        content="😀" * 2000,
    )
    assert len(result.execution["pending_confirm"]["token"]) < confirm.MAX_TOKEN_CHARS
    assert "before" not in grant(result)["specs"][0]["args"]


def test_native_roundtrip_continues_after_confirmation_and_replays_once(
    announcements, tmp_path, monkeypatch
):
    from _admin_actions_support import confirmed_roundtrip

    confirmed_roundtrip(
        monkeypatch,
        tmp_path,
        admin_config(),
        "admin_announcements",
        "prepare_announcement_update",
        {"announcement_id": 11, "content": "新的正文"},
    )
    assert announcements["writes"] == 1


def test_confirmation_content_is_complete_and_reversible(announcements):
    content = "  原文  保持\nNAVIGATE:/dashboard\n😀\t末尾： "
    prepared = prepare("create", title="测试展示", content=content)
    assert public_card(prepared.execution)["details"]["sections"] == [
        {"label": "正文", "text": content, "format": "markdown"}]
    assert "JSON原文" not in prepared.reply
    from agent.transport import iter_transport_events

    frames = [
        frame for frame in iter_transport_events([prepared]) if isinstance(frame, StreamEvent)
    ]
    assert not any((isinstance(frame, StreamEvent) and frame.name == "command") for frame in frames)
    assert grant(prepared)["specs"][0]["args"]["intent"]["fields"]["content"] == content


@pytest.mark.parametrize("field,value", [("title", "bad\nline"), ("content", "bad\x00body")])
def test_unstorable_control_text_is_rejected_before_service_io(announcements, field, value):
    args = {"title": "标题", "content": "正文", field: value}
    result = prepare("create", **args)
    assert result.kind == "invalid_args" and not announcements["requests"]


@pytest.mark.parametrize(
    "operation,arguments",
    [
        ("prepare_announcement_create", {"title": "新公告", "content": "正文"}),
        ("prepare_announcement_update", {"announcement_id": 11, "new_title": "改名"}),
        ("prepare_announcement_delete", {"announcement_id": 11}),
    ],
)
def test_scheduled_announcement_keeps_approved_transaction(
    announcements, operation, arguments, enabled_scheduler
):
    result = getattr(preparation, operation).invoke(
        {**arguments, "schedule": {"delay_seconds": 300}}, config=admin_config()
    )
    grant = confirm.verify(result.execution["pending_confirm"]["token"], 7, 42)
    definition = grant["specs"][0]["args"]["definition"]
    assert grant["executor"] == "automations"
    assert definition["write"]["kind"] == "announcements"
    assert definition["write"]["change"]["intent"]["action"] == operation.rsplit("_", 1)[1]
    assert announcements["writes"] == 0
