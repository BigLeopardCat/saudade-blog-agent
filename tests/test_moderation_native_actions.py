"""Native moderation uses signed versions and actual HTTP readback evidence."""

import copy
import hashlib
import json
import secrets

import httpx
import pytest
from _own_support import config
from pydantic import ValidationError

from agent import confirm, idem
from agent.domains.moderation_actions import preparation, transactions
from agent.principal import SOURCE_ASSERTION, Principal
from agent.runtime import writes
from agent.runtime.pending import PendingOperation
from agent.runtime.results import TurnResult
from tools import base


def admin_config(**values):
    return config(principal=Principal(7, "admin", SOURCE_ASSERTION), **values)


def _row(resource):
    return {
        "id": 11,
        "userId": 8,
        "publisher": {"id": 8, "username": "author", "nickname": "真实发表账号", "role": "user"},
        "displayName": "自由留名" if resource == "boards" else None,
        "articleId": 19 if resource == "comments" else None,
        "articleTitle": "真实文章" if resource == "comments" else None,
        "content": "真实正文，不是模糊片段",
        "contentTruncated": False,
        "approved": 0,
        "isDeleted": False,
        "affectedReplies": 2 if resource == "comments" else 0,
        "publicReplies": 0,
        "rootVisible": True,
        "articleVisible": True,
        "rejectReason": None,
        "aiReason": "保留的审核说明",
    }


def _version(row):
    return hashlib.sha256(json.dumps(row, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def _rejection_evidence(state, row, action, notice):
    reason = next(
        (
            value.strip()
            for value in (row["rejectReason"], row["aiReason"])
            if isinstance(value, str) and value.strip()
        ),
        None,
    )
    if notice and action == "reject":
        state["notice_reasons"].append(reason)
    return reason


@pytest.fixture
def moderation(monkeypatch):
    state = {
        "boards": {11: _row("boards")},
        "comments": {11: _row("comments")},
        "requests": [],
        "writes": 0,
        "notices": 0,
        "notice_reasons": [],
        "ignore": False,
        "after_unavailable": False,
        "lost_response": False,
        "cas_race": False,
        "wrong_actor": False,
        "malformed": False,
    }

    def handle(request):
        state["requests"].append(request)
        assert request.headers.get("Authorization", "").startswith("Bearer ")
        if request.url.path.endswith("/todos"):
            return httpx.Response(200, json={"code": 200, "data": []})
        pieces = request.url.path.split("/")
        resource, identifier, endpoint = pieces[-3], int(pieces[-2]), pieces[-1]
        row = state[resource].get(identifier)
        if request.method == "GET":
            assert endpoint == "target"
            if state["after_unavailable"] and state["writes"]:
                return httpx.Response(503)
            data = {
                "resource": resource,
                "id": identifier,
                "exists": row is not None,
                "target": {
                    **copy.deepcopy(row),
                    "version": state.get("version_override", _version(row)),
                }
                if row
                else None,
            }
            if state["malformed"]:
                data["exists"] = False
            return httpx.Response(200, json={"code": 200, "data": data})
        assert request.method == "POST" and endpoint == "action"
        state["writes"] += 1
        payload = json.loads(request.content)
        assert set(payload) == {"action", "version"}
        if state["cas_race"]:
            row["content"] = "提交前正文已变化"
        if row is None or payload["version"] != _version(row):
            return httpx.Response(200, json={"code": 409, "message": "目标已变化", "data": {}})
        action = payload["action"]
        approved = row["approved"] if action == "delete" else 1 if action == "pass" else 2
        changed = not row["isDeleted"] and (action == "delete" or approved != row["approved"])
        notice = int(changed and action != "delete" and resource == "boards")
        state["notices"] += notice
        reason = _rejection_evidence(state, row, action, notice)
        data = {
            "resource": resource,
            "id": identifier,
            "action": action,
            "actorId": 99 if state["wrong_actor"] else 7,
            "confirmedVersion": payload["version"],
            "changed": changed,
            "approved": approved,
            "isDeleted": action == "delete",
            "notifications": notice,
        }
        if not state["ignore"]:
            if action == "delete" and resource == "boards":
                del state[resource][identifier]
            elif action == "delete":
                row["isDeleted"] = True
            else:
                row["approved"] = approved
                row["rejectReason"] = None if action == "pass" else reason
        if state["lost_response"]:
            raise httpx.ReadTimeout("request sent; response lost")
        return httpx.Response(200, json={"code": 200, "data": data})

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        monkeypatch.setattr(base, "_client", client)
        monkeypatch.setattr(confirm.settings, "jwt_secret", secrets.token_urlsafe(32))
        yield state


def prepare(resource="board", action="pass", **kwargs):
    return preparation.prepare_moderation_change.invoke(
        {"resource": resource, "action": action, "item_id": 11, **kwargs}, config=admin_config()
    )


def grant(result):
    return confirm.verify(result.execution["pending_confirm"]["token"], 7, 42)


@pytest.mark.parametrize(
    "resource,action,workflow,name,key",
    [
        ("board", "pass", "board_audit", "audit_board_comment", "talk_id"),
        ("board", "delete", "board_delete", "delete_board_comment", "talk_id"),
        ("comment", "reject", "comment_audit", "audit_comment", "comment_id"),
        ("comment", "delete", "comment_delete", "delete_comment", "comment_id"),
    ],
)
def test_preparation_is_zero_write_and_signs_exact_collection_version(
    moderation, resource, action, workflow, name, key
):
    result = prepare(resource, action)
    assert isinstance(result, TurnResult)
    signed = grant(result)
    assert signed["executor"] == "moderation" and signed["workflow"] == workflow
    expected = {
        key: 11,
        "version": _version(moderation["boards" if resource == "board" else "comments"][11]),
    }
    if action != "delete":
        expected["verdict"] = action
    assert signed["specs"] == [{"tool": name, "args": expected}]
    question = result.execution["pending_confirm"]["q"]
    assert "真实正文" in question and "UID 8" in question and "真实发表账号" in question
    if resource == "comment":
        assert "真实文章" in question and "关联 2 条" in question
    if action == "delete":
        assert "不可恢复" in question or "无恢复入口" in question
    assert all(request.method == "GET" for request in moderation["requests"])
    assert moderation["writes"] == 0 and not result.execution["receipts"]
    assert result.execution["pending_confirm"]["token"] not in result.reply


@pytest.mark.parametrize(
    "resource,action",
    [
        ("board", "pass"),
        ("board", "reject"),
        ("board", "delete"),
        ("comment", "pass"),
        ("comment", "reject"),
        ("comment", "delete"),
    ],
)
def test_real_writes_recheck_exact_state_and_replay_without_duplicate_notifications(
    moderation, tmp_path, resource, action
):
    signed = grant(prepare(resource, action))
    ledger = idem.Ledger(str(tmp_path / "writes.sqlite3"))
    first = transactions.execute(signed, admin_config(), None, ledger=ledger)
    second = transactions.execute(signed, admin_config(), None, ledger=ledger)
    assert moderation["writes"] == 1
    assert len(first["receipts"]) == len(second["receipts"]) == 1
    assert first["reply"] == second["reply"] and not first["blocked"]
    assert first["receipts"][0]["args"] == signed["specs"][0]["args"]
    if resource == "board" and action == "delete":
        assert 11 not in moderation["boards"]
    elif action == "delete":
        assert moderation["comments"][11]["isDeleted"] is True
    else:
        assert moderation["boards" if resource == "board" else "comments"][11]["approved"] == (
            1 if action == "pass" else 2
        )
        assert moderation["notices"] == int(resource == "board")


@pytest.mark.parametrize(
    "field,value",
    [
        ("content", "新的正文"),
        ("userId", 9),
        ("affectedReplies", 3),
        ("rootVisible", False),
        ("articleVisible", False),
        ("publisher", {"id": 8, "username": "renamed", "nickname": "新名字", "role": "user"}),
    ],
)
def test_changed_target_or_impact_prevents_business_write(moderation, tmp_path, field, value):
    signed = grant(prepare("comment", "delete"))
    moderation["comments"][11][field] = value
    result = transactions.execute(
        signed, admin_config(), None, ledger=idem.Ledger(str(tmp_path / "stale.sqlite3"))
    )
    assert moderation["writes"] == 0 and not result["receipts"] and result["blocked"]


@pytest.mark.parametrize(
    "mode", ["ignore", "after_unavailable", "lost_response", "cas_race", "wrong_actor"]
)
def test_unknown_or_rejected_write_never_claims_completion_or_repeats(moderation, tmp_path, mode):
    signed = grant(prepare("board", "delete"))
    moderation[mode] = True
    ledger = idem.Ledger(str(tmp_path / "uncertain.sqlite3"))
    first = transactions.execute(signed, admin_config(), None, ledger=ledger)
    second = transactions.execute(signed, admin_config(), None, ledger=ledger)
    assert moderation["writes"] == 1
    assert not first["receipts"] and not second["receipts"]
    assert first["blocked"] and second["blocked"]


@pytest.mark.parametrize(
    "role,source", [("user", SOURCE_ASSERTION), ("admin", "body"), ("superadmin", "body")]
)
def test_non_console_or_unverified_actor_never_reads_or_prepares(moderation, role, source):
    result = preparation.prepare_moderation_change.invoke(
        {"resource": "board", "action": "delete", "item_id": 11},
        config=config(principal=Principal(7, role, source)),
    )
    assert result.kind == "unavailable" and not moderation["requests"]


@pytest.mark.parametrize(
    "values", [{"user_id": 8}, {"thread_id": "conv_43"}, {"conversation_id": None}]
)
def test_missing_stable_binding_is_zero_read(moderation, values):
    result = preparation.prepare_moderation_change.invoke(
        {"resource": "board", "action": "delete", "item_id": 11}, config=admin_config(**values)
    )
    assert result.kind == "unavailable" and not moderation["requests"]


@pytest.mark.parametrize(
    "change",
    [
        {"uid": 8},
        {"conv": 43},
        {"exp": 1},
        {"executor": "todos"},
        {"specs": []},
        {"workflow": "comment_delete"},
    ],
)
def test_forged_binding_never_claims_a_ledger_or_calls(moderation, change):
    signed = {**grant(prepare()), **change}
    before = len(moderation["requests"])

    class NoLedger:
        def claim(self, *args, **kwargs):
            pytest.fail("invalid confirmation reached ledger")

    result = transactions.execute(signed, admin_config(), None, ledger=NoLedger())
    assert not result["receipts"] and result["blocked"]
    assert len(moderation["requests"]) == before


@pytest.mark.parametrize(
    "field,value",
    [
        ("contentTruncated", True),
        ("version", None),
        ("approved", True),
        ("publicReplies", 3),
        ("content", "x" * 501),
    ],
)
def test_incomplete_targets_do_not_generate_a_card(moderation, field, value):
    if field == "version":
        moderation["version_override"] = value
    else:
        moderation["comments"][11][field] = value
    result = prepare("comment", "delete")
    assert result.kind == "unavailable" and not moderation["writes"]


def test_unproved_absence_is_unavailable_not_deleted(moderation):
    moderation["malformed"] = True
    result = prepare()
    assert result.kind == "unavailable"


@pytest.mark.parametrize("resource,action", [("board", "pass"), ("comment", "delete")])
def test_satisfied_state_needs_no_card_or_business_write(moderation, resource, action):
    if resource == "board":
        moderation["boards"][11]["approved"] = 1
    else:
        moderation["comments"][11]["isDeleted"] = True
    result = prepare(resource, action)
    assert result.execution["noop_note"] == result.reply
    assert "pending_confirm" not in result.execution and not moderation["writes"]


def test_deleted_comment_cannot_be_audited(moderation):
    moderation["comments"][11]["isDeleted"] = True
    assert prepare("comment", "pass").kind == "unavailable"


@pytest.mark.parametrize(
    "args", [{"item_id": True}, {"item_id": "11"}, {"action": "approve"}, {"resource": "article"}]
)
def test_schema_rejects_coercion_and_unknown_directions(moderation, args):
    with pytest.raises(ValidationError):
        prepare(**args)
    assert not moderation["requests"]


def test_cancel_and_cross_domain_replacement_revoke_old_confirmation(
    moderation, monkeypatch, tmp_path
):
    from agent.domains import registry
    from agent.domains.todos import preparation as todos

    monkeypatch.setitem(registry._DOMAINS, "moderation", transactions)
    ledger = idem.Ledger(str(tmp_path / "revoke.sqlite3"))
    monkeypatch.setattr(writes, "ledger_for", lambda saver: ledger)
    initial = prepare("board", "delete")
    pending = PendingOperation(grant(initial), initial.execution["pending_action"])
    replacement = todos.prepare_dashboard_todo.invoke(
        {"action": "create", "text": "准备面试"}, config=admin_config()
    )
    moved = pending.transition(replacement, admin_config(), None)
    assert moved.execution["pending_event"]["event"] == "cancelled"
    assert grant(moved)["executor"] == "todos"
    old = transactions.execute(grant(initial), admin_config(), None, ledger=ledger)
    assert not old["receipts"] and not moderation["writes"]
    next_card = prepare("comment", "reject")
    pending = PendingOperation(grant(next_card), next_card.execution["pending_action"])
    cancelled = pending.cancel_tool(None).invoke({}, config=admin_config())
    assert cancelled.execution["pending_action"] is None
    replay = transactions.execute(grant(next_card), admin_config(), None, ledger=ledger)
    assert not replay["receipts"] and not moderation["writes"]


def test_crash_after_write_leaves_unknown_intent_instead_of_repeating(
    moderation, monkeypatch, tmp_path
):
    signed = grant(prepare("board", "delete"))
    original = transactions.Operation.invoke

    def crash(self, args, config=None):
        original(self, args, config)
        raise SystemExit("worker stopped after business write")

    monkeypatch.setattr(transactions.Operation, "invoke", crash)
    ledger = idem.Ledger(str(tmp_path / "crash.sqlite3"))
    with pytest.raises(SystemExit):
        transactions.execute(signed, admin_config(), None, ledger=ledger)
    replay = transactions.execute(signed, admin_config(), None, ledger=ledger)
    assert moderation["writes"] == 1 and not replay["receipts"]
    assert "结果未知" in replay["reply"]


@pytest.mark.parametrize(
    "resource,action",
    [
        ("board", "pass"),
        ("board", "delete"),
        ("comment", "pass"),
        ("comment", "delete"),
    ],
)
def test_native_confirmation_continues_after_execution_and_replays_once(
    moderation, monkeypatch, tmp_path, resource, action
):
    from _admin_actions_support import confirmed_roundtrip

    confirmed_roundtrip(
        monkeypatch,
        tmp_path,
        admin_config(),
        "admin_moderation",
        "prepare_moderation_change",
        {"resource": resource, "action": action, "item_id": 11},
    )
    assert moderation["writes"] == 1


@pytest.mark.parametrize("resource", ["board", "comment"])
@pytest.mark.parametrize(
    "saved,expected", [("", "具体AI理由"), (" \t\n ", "具体AI理由"), ("  手工理由  ", "手工理由")]
)
def test_card_and_actual_rejection_use_the_same_nonblank_reason(
    moderation, tmp_path, resource, saved, expected
):
    collection = "boards" if resource == "board" else "comments"
    moderation[collection][11].update(rejectReason=saved, aiReason="  具体AI理由  ")
    prepared = prepare(resource, "reject")
    assert f"沿用已有理由：{expected}。" in prepared.execution["pending_confirm"]["q"]
    result = transactions.execute(
        grant(prepared), admin_config(), None, ledger=idem.Ledger(str(tmp_path / "reason.sqlite3"))
    )
    assert result["receipts"] and moderation["writes"] == 1
    assert moderation[collection][11]["rejectReason"] == expected
    assert moderation["notice_reasons"] == ([expected] if resource == "board" else [])
