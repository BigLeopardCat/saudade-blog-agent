"""Announcement changes bind exact targets, fields and scope to native approvals."""

import copy
import hashlib
import json
import secrets

import httpx
import pytest
from _own_support import config
from pydantic import ValidationError

from agent import confirm, idem
from agent.domains.announcements import preparation, transactions
from agent.principal import SOURCE_ASSERTION, Principal
from agent.products.blog.pending import PendingOperation
from agent.runtime import writes
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
        payload = json.loads(request.content)
        if request.url.path.endswith("/prepare"):
            prepared = plan(payload)
            if state.get("wrong_plan"):
                prepared["after"]["title"] = "unrequested"
            return httpx.Response(200, json={"code": 200, "data": prepared})
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
        if state.get("lost"):
            raise httpx.ReadTimeout("response lost after commit")
        result = {
            "changed": current["before"] != current["after"],
            "beforeVersion": payload["version"],
            "state": current,
        }
        if state.get("wrong_result"):
            result["state"]["notificationCount"] += 1
        return httpx.Response(200, json={"code": 200, "data": result})

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        monkeypatch.setattr(base, "_client", client)
        monkeypatch.setattr(confirm.settings, "jwt_secret", secrets.token_urlsafe(32))
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
    assert "2 条关联个人通知" in prepared.reply
    if action == "update":
        assert "新的正文" in prepared.reply and "原文 保持" in prepared.reply
    if action == "delete":
        assert "及其关联通知" in prepared.reply and "#11" in prepared.reply
    ledger = idem.Ledger(str(tmp_path / "once.sqlite3"))
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
        signed, admin_config(), None, ledger=idem.Ledger(str(tmp_path / "stale.sqlite3"))
    )
    assert not result["receipts"] and not announcements["writes"]


@pytest.mark.parametrize("mode", ["lost", "wrong_result"])
def test_lost_or_mismatched_result_is_not_repeated(announcements, tmp_path, mode):
    signed = grant(prepare("update", announcement_id=11, content="新的正文"))
    announcements[mode] = True
    ledger = idem.Ledger(str(tmp_path / "unknown.sqlite3"))
    for _ in range(2):
        assert not transactions.execute(signed, admin_config(), None, ledger=ledger)["receipts"]
    assert announcements["writes"] == 1 and announcements["rows"][11]["content"] == "新的正文"


def test_cancelled_card_is_durable_and_cannot_write(announcements, tmp_path, monkeypatch):
    prepared = prepare("delete", announcement_id=11)
    signed = grant(prepared)
    store = idem.Ledger(str(tmp_path / "revoked.sqlite3"))
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
    assert prepare("delete", announcement_id=11,
                   announcement_title="测试").execution["pending_confirm"]
    assert prepare("delete", announcement_id=11,
                   announcement_title="不匹配").kind == "unavailable"
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
    ledger = idem.Ledger(str(tmp_path / "denied.sqlite3"))
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
    assert "正文：\n\n" + content in prepared.reply
    assert "JSON原文" not in prepared.reply
    from agent.transport import iter_transport_events
    frames = [frame for frame in iter_transport_events([prepared]) if isinstance(frame, str)]
    assert not any(frame.startswith("__CMD__:") for frame in frames)
    assert grant(prepared)["specs"][0]["args"]["intent"]["fields"]["content"] == content


@pytest.mark.parametrize("field,value", [("title", "bad\nline"), ("content", "bad\x00body")])
def test_unstorable_control_text_is_rejected_before_service_io(announcements, field, value):
    args = {"title": "标题", "content": "正文", field: value}
    result = prepare("create", **args)
    assert result.kind == "invalid_args" and not announcements["requests"]
