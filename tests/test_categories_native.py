"""Category changes bind exact targets, fields and scope to native approvals."""

import copy
import hashlib
import json
import secrets

import httpx
import pytest
from _own_support import config
from pydantic import ValidationError

from agent import confirm, idem
from agent.domains.categories import preparation, transactions
from agent.principal import SOURCE_ASSERTION, Principal
from agent.products.blog.pending import PendingOperation
from agent.runtime import writes
from tools import base


def admin_config(**values):
    return config(principal=Principal(uid=7, role="admin", source=SOURCE_ASSERTION), **values)


@pytest.fixture
def categories(monkeypatch):
    state = {
        "rows": {
            11: {
                "id": 11,
                "name": "测试",
                "pathName": "test",
                "introduce": "intro",
                "icon": "icon",
                "color": "#123456",
            }
        },
        "scope": [(101, None), (102, 101)],
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
                if row["id"] == target["id"] or row["name"] == target["name"]
            ]
            assert len(found) == 1
            before = copy.deepcopy(found[0])
        after = (
            copy.deepcopy(before)
            if before
            else dict.fromkeys(("id", "name", "pathName", "introduce", "icon", "color"))
        )
        if intent["action"] == "delete":
            after = None
        else:
            after.update(
                {key: value for key, value in intent["fields"].items() if value is not None}
            )
            if before is None and after["pathName"] is None:
                after["pathName"] = after["name"]
        scope = state["scope"] if before else []
        version = hashlib.sha256(
            json.dumps([intent, before, after, scope, state["revision"]], sort_keys=True).encode()
        ).hexdigest()
        return {
            "version": version,
            "before": before,
            "after": after,
            "articleCount": sum(revision is None for _, revision in scope),
            "revisionCount": sum(revision is not None for _, revision in scope),
        }

    def handle(request):
        state["requests"].append(request)
        payload = json.loads(request.content)
        if request.url.path.endswith("/prepare"):
            prepared = plan(payload)
            if state.get("wrong_plan"):
                prepared["after"]["pathName"] = "unrequested"
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
            result["state"]["articleCount"] += 1
        return httpx.Response(200, json={"code": 200, "data": result})

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        monkeypatch.setattr(base, "_client", client)
        monkeypatch.setattr(confirm.settings, "jwt_secret", secrets.token_urlsafe(32))
        yield state


def grant(prepared):
    return confirm.verify(prepared.execution["pending_confirm"]["token"], 7, 42)


def prepare(action, **args):
    return getattr(preparation, "prepare_category_" + action).invoke(args, config=admin_config())


@pytest.mark.parametrize(
    "action,args,tool",
    [
        ("create", {"name": "新分类"}, "create_category"),
        (
            "update",
            {"category_name": "测试", "new_name": "演示", "introduce": "", "icon": "", "color": ""},
            "update_category",
        ),
        ("delete", {"category_id": 11}, "delete_category"),
    ],
)
def test_prepare_is_read_only_and_confirmed_replay_is_one_write(
    categories, tmp_path, action, args, tool
):
    prepared = prepare(action, **args)
    signed = grant(prepared)
    assert signed["executor"] == "categories" and signed["specs"][0]["tool"] == tool
    assert set(signed["specs"][0]["args"]) == {"version", "intent"}
    assert not categories["writes"]
    if action != "create":
        assert "1 篇文章" in prepared.reply and "1 份编辑草稿" in prepared.reply
    if action == "update":
        assert "（空）" in prepared.reply and "intro" in prepared.reply
    if action == "delete":
        assert "文章不删除" in prepared.reply and "#11" in prepared.reply
    ledger = idem.Ledger(str(tmp_path / "once.sqlite3"))
    for _ in range(2):
        result = transactions.execute(signed, admin_config(), None, ledger=ledger)
        assert len(result["receipts"]) == 1 and not result["blocked"]
    assert categories["writes"] == 1
    if action == "create":
        assert categories["rows"][12]["name"] == "新分类"
    elif action == "update":
        assert categories["rows"][11]["introduce"] == ""
        assert categories["rows"][11]["pathName"] == "test"
    else:
        assert 11 not in categories["rows"]


def test_noop_does_not_create_a_card(categories):
    result = prepare("update", category_id=11, introduce="intro")
    assert result.execution["noop_note"] and not result.execution.get("pending_confirm")
    assert not categories["writes"]


@pytest.mark.parametrize("mode", ["scope", "row", "race"])
def test_changed_category_or_scope_never_executes_a_stale_approval(categories, tmp_path, mode):
    signed = grant(prepare("delete", category_name="测试"))
    if mode == "scope":
        categories["scope"][0] = (103, None)
    elif mode == "row":
        categories["rows"][11]["introduce"] = "changed without timestamp"
    else:
        categories["race"] = True
    result = transactions.execute(
        signed, admin_config(), None, ledger=idem.Ledger(str(tmp_path / "stale.sqlite3"))
    )
    assert not result["receipts"] and not categories["writes"]


@pytest.mark.parametrize("mode", ["lost", "wrong_result"])
def test_lost_or_mismatched_result_is_not_repeated(categories, tmp_path, mode):
    signed = grant(prepare("update", category_id=11, introduce=""))
    categories[mode] = True
    ledger = idem.Ledger(str(tmp_path / "unknown.sqlite3"))
    for _ in range(2):
        assert not transactions.execute(signed, admin_config(), None, ledger=ledger)["receipts"]
    assert categories["writes"] == 1 and categories["rows"][11]["introduce"] == ""


def test_cancelled_card_is_durable_and_cannot_write(categories, tmp_path, monkeypatch):
    prepared = prepare("delete", category_id=11)
    signed = grant(prepared)
    store = idem.Ledger(str(tmp_path / "revoked.sqlite3"))
    monkeypatch.setattr(writes, "ledger_for", lambda saver: store)
    pending = PendingOperation(signed, prepared.execution["pending_action"])
    assert pending.cancel(admin_config(), None).execution["pending_event"]["event"] == "cancelled"
    assert not transactions.execute(signed, admin_config(), None, ledger=store)["receipts"]
    assert not categories["writes"]


@pytest.mark.parametrize(
    "args", [{}, {"category_id": 11, "category_name": "测试"}, {"category_name": " "}]
)
def test_invalid_target_is_rejected_before_io(categories, args):
    result = prepare("delete", **args)
    assert result.kind == "invalid_args" and not categories["requests"]


def test_unrequested_plan_change_cannot_issue_a_card(categories):
    categories["wrong_plan"] = True
    assert prepare("update", category_id=11, introduce="").kind == "unavailable"
    assert not categories["writes"]


@pytest.mark.parametrize(
    "principal",
    [Principal(uid=7, role="admin"), Principal(uid=7, role="user", source=SOURCE_ASSERTION)],
)
def test_unverified_or_unauthorized_actor_cannot_prepare(categories, principal):
    result = preparation.prepare_category_delete.invoke(
        {"category_id": 11}, config=config(principal=principal)
    )
    assert result.kind == "unavailable" and not categories["requests"]


def test_permission_revocation_and_malformed_specs_cannot_execute(categories, tmp_path):
    signed = grant(prepare("delete", category_id=11))
    revoked = config(principal=Principal(uid=7, role="user", source=SOURCE_ASSERTION))
    ledger = idem.Ledger(str(tmp_path / "denied.sqlite3"))
    assert not transactions.execute(signed, revoked, None, ledger=ledger)["receipts"]
    for specs in [
        [None],
        [
            {
                "tool": "delete_category",
                "args": {
                    "version": "a" * 64,
                    "intent": {"action": "delete", "target": {"id": True}},
                },
            }
        ],
    ]:
        with pytest.raises((ValueError, ValidationError)):
            transactions.validate({**signed, "specs": specs}, admin_config())
    assert not categories["writes"]


def test_maximum_create_keeps_confirmation_token_bounded(categories):
    result = prepare(
        "create",
        name="n" * 40,
        path_name="p" * 255,
        introduce="i" * 255,
        icon="x" * 255,
        color="#123456",
    )
    assert len(result.execution["pending_confirm"]["token"]) < 3000
    assert "before" not in grant(result)["specs"][0]["args"]


def test_native_roundtrip_continues_after_confirmation_and_replays_once(
    categories, tmp_path, monkeypatch
):
    from _admin_actions_support import confirmed_roundtrip

    confirmed_roundtrip(
        monkeypatch,
        tmp_path,
        admin_config(),
        "admin_categories",
        "prepare_category_update",
        {"category_id": 11, "introduce": ""},
    )
    assert categories["writes"] == 1
