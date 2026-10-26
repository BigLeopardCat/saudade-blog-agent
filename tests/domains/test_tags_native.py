"""Dictionary changes preserve typed identity, signed scope and durable replay."""

import copy
import hashlib
import json

import httpx
import pytest
from _own_support import config

from agent import confirm, idem
from agent.domains.tags import preparation, targets, transactions
from agent.principal import SOURCE_ASSERTION, Principal
from agent.runtime import writes
from tools import base


def admin_config(**values):
    return config(principal=Principal(uid=7, role="admin", source=SOURCE_ASSERTION), **values)


@pytest.fixture
def tags(monkeypatch):
    state = {
        "rows": {
            (1, 11): {"id": 11, "level": 1, "name": "测试", "color": None, "parentId": None},
            (2, 11): {"id": 11, "level": 2, "name": "子标签", "color": None, "parentId": 12},
            (1, 12): {"id": 12, "level": 1, "name": "父级", "color": None, "parentId": None},
        },
        "scope": [(101, 0)],
        "writes": 0,
        "requests": [],
        "revision": 0,
    }

    def resolve(t):
        hits = [r for r in state["rows"].values() if targets.matches(t, r)]
        if len(hits) != 1:
            raise ValueError("missing or ambiguous")
        return copy.deepcopy(hits[0])

    def plan(i):
        before = resolve(i["target"]) if i["target"] else None
        after = (
            copy.deepcopy(before)
            if before
            else {"id": None, "level": 1, "name": "", "color": None, "parentId": None}
        )
        parent = None
        if i["action"] == "delete":
            after = None
        else:
            f = i["fields"]
            after.update({k: f[k] for k in ("name", "color", "level") if f[k] is not None})
            if f["parent"]:
                parent = resolve(f["parent"])
                after.update(level=2, parentId=parent["id"])
            if f["level"] == 1:
                after["parentId"] = None
            elif after["parentId"] and parent is None:
                parent = resolve({"id": after["parentId"], "level": 1})
            if before and before["level"] != after["level"]:
                after["id"] = None
        version = hashlib.sha256(
            json.dumps(
                [i, before, after, parent, state["scope"], state["revision"]], sort_keys=True
            ).encode()
        ).hexdigest()
        return {
            "version": version,
            "before": before,
            "after": after,
            "parent": parent,
            "childCount": 0,
            "articleCount": int(before is not None),
            "revisionCount": 0,
        }

    def handle(req):
        state["requests"].append(req)
        payload = json.loads(req.content)
        try:
            if req.url.path.endswith("/prepare"):
                p = plan(payload)
                if state.get("wrong_plan") and p["after"]:
                    p["after"]["name"] = "unrequested"
                return httpx.Response(200, json={"code": 200, "data": p})
            if state.get("race"):
                state["revision"] += 1
            p = plan(payload["intent"])
            if p["version"] != payload["version"]:
                return httpx.Response(200, json={"code": 409, "message": "stale"})
            changed = p["before"] != p["after"]
            if p["before"]:
                state["rows"].pop((p["before"]["level"], p["before"]["id"]))
            if p["after"]:
                p["after"]["id"] = p["after"]["id"] or 21
                state["rows"][(p["after"]["level"], p["after"]["id"])] = copy.deepcopy(p["after"])
            state["writes"] += 1
            if state.get("lost"):
                raise httpx.ReadTimeout("lost after commit")
            if state.get("wrong_result"):
                p["articleCount"] += 1
            return httpx.Response(
                200,
                json={
                    "code": 200,
                    "data": {"changed": changed, "beforeVersion": payload["version"], "state": p},
                },
            )
        except ValueError:
            return httpx.Response(200, json={"code": 400, "message": "missing or ambiguous"})

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        monkeypatch.setattr(base, "_client", client)
        monkeypatch.setattr(
            "config.settings.jwt_secret", "tag-native-test-secret-at-least-32-bytes"
        )
        yield state


def grant(result):
    token = result.execution["pending_confirm"]["token"]
    return confirm.verify(token, 7, 42)


@pytest.mark.parametrize(
    "action,args",
    [
        ("create", {"name": "新标签", "parent_id": 12}),
        ("update", {"tag_id": 11, "level": 1, "parent_id": 12}),
        ("update", {"tag_id": 11, "level": 2, "to_level": 1, "color": ""}),
        ("delete", {"tag_id": 11, "level": 1}),
    ],
)
def test_preparation_is_readonly_and_confirmation_replays_once(tags, tmp_path, action, args):
    cfg = admin_config()
    result = getattr(preparation, "prepare_tag_" + action).invoke(args, config=cfg)
    g = grant(result)
    assert g and set(g["specs"][0]["args"]) == {"version", "intent"}
    assert tags["writes"] == 0 and len(tags["requests"]) == 1
    ledger = idem.Ledger(str(tmp_path / "ledger.sqlite3"))
    first = transactions.execute(g, cfg, None, ledger=ledger)
    second = transactions.execute(g, cfg, None, ledger=ledger)
    assert first.get("receipts") and second.get("receipts")
    assert tags["writes"] == 1
    # The other level's numerically identical tag is not the target.
    if action == "delete":
        assert (2, 11) in tags["rows"]


@pytest.mark.parametrize("failure", ["stale", "race", "lost", "wrong_result"])
def test_changed_scope_or_uncertain_result_never_retries(tags, tmp_path, failure):
    cfg = admin_config()
    g = grant(
        preparation.prepare_tag_update.invoke(
            {"tag_id": 11, "level": 1, "new_name": "改名"}, config=cfg
        )
    )
    if failure == "stale":
        tags["scope"] = [(102, 0)]
    else:
        tags[failure] = True
    ledger = idem.Ledger(str(tmp_path / "ledger.sqlite3"))
    for _ in range(2):
        result = transactions.execute(g, cfg, None, ledger=ledger)
        assert not result.get("receipts")
    assert tags["writes"] == int(failure in ("lost", "wrong_result"))


def test_wrong_service_plan_cannot_authorize_extra_changes(tags):
    tags["wrong_plan"] = True
    result = preparation.prepare_tag_update.invoke(
        {"tag_name": "测试", "color": "#123456"}, config=admin_config()
    )
    assert isinstance(result, base.ToolResult) and result.kind != "ok"
    assert tags["writes"] == 0


def test_target_pair_preserves_tag_namespace_and_rejects_conflicting_name(tags):
    matched = preparation.prepare_tag_delete.invoke(
        {"tag_id": 11, "level": 1, "tag_name": "测试"}, config=admin_config())
    assert matched.execution["pending_confirm"] and not tags["writes"]
    mismatch = preparation.prepare_tag_delete.invoke(
        {"tag_id": 11, "level": 1, "tag_name": "子标签"}, config=admin_config())
    assert isinstance(mismatch, base.ToolResult) and not tags["writes"]


def test_exact_names_reject_ambiguity_and_unauthorized_inputs_do_no_io(tags):
    cfg = admin_config()
    tags["rows"][(2, 11)]["name"] = "测试"
    assert isinstance(
        preparation.prepare_tag_delete.invoke({"tag_name": "测试"}, config=cfg), base.ToolResult
    )
    before = len(tags["requests"])
    for args in [
        {"tag_id": 11},
        {"tag_id": 11, "level": True},
        {"tag_id": 11, "level": 1, "tag_name": " "},
    ]:
        if args.get("level") is True:
            with pytest.raises(ValueError):
                preparation.prepare_tag_delete.invoke(args, config=cfg)
        else:
            assert isinstance(
                preparation.prepare_tag_delete.invoke(args, config=cfg), base.ToolResult
            )
    assert len(tags["requests"]) == before
    result = preparation.prepare_tag_delete.invoke(
        {"tag_id": 11, "level": 1},
        config=config(principal=Principal(uid=7, role="user", source=SOURCE_ASSERTION)),
    )
    assert isinstance(result, base.ToolResult) and len(tags["requests"]) == before


def test_noop_and_cancel_do_not_write(tags, tmp_path, monkeypatch):
    from agent.products.blog.pending import PendingOperation

    cfg = admin_config()
    result = preparation.prepare_tag_update.invoke(
        {"tag_id": 11, "level": 1, "new_name": "测试"}, config=cfg
    )
    assert not result.execution.get("pending_confirm")
    prepared = preparation.prepare_tag_delete.invoke({"tag_id": 11, "level": 1}, config=cfg)
    g = grant(prepared)
    ledger = idem.Ledger(str(tmp_path / "ledger.sqlite3"))
    monkeypatch.setattr(writes, "ledger_for", lambda saver: ledger)
    pending = PendingOperation(g, prepared.execution["pending_action"])
    assert pending.cancel(cfg, None).execution["pending_event"]["event"] == "cancelled"
    result = transactions.execute(g, cfg, None, ledger=ledger)
    assert not result.get("receipts") and tags["writes"] == 0


def test_native_roundtrip_continues_after_confirmation_and_replays_once(
        tags, tmp_path, monkeypatch):
    from _admin_actions_support import confirmed_roundtrip

    confirmed_roundtrip(
        monkeypatch,
        tmp_path,
        admin_config(),
        "admin_tags",
        "prepare_tag_update",
        {"tag_id": 11, "level": 2, "to_level": 1},
    )
    assert tags["writes"] == 1
