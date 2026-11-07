"""Notification acknowledgements require complete owned transaction facts."""

import copy
import json
from contextlib import closing

import pytest
from _own_support import config

from agent import confirm
from agent.domains.notifications import preparation, transactions
from agent.runtime.results import TurnResult
from eval.fixtures.ledger import Ledger


def prepare():
    result = preparation.prepare_notification_read.invoke(
        {"scope": "selected", "ids": [101, 102]}, config=config())
    assert isinstance(result, TurnResult)
    return confirm.verify(result.execution["pending_confirm"]["token"], 7, 42)


def execute_twice(notifications, tmp_path, grant):
    with closing(Ledger(str(tmp_path / "notification-writes.sqlite3"))) as ledger:
        first = transactions.execute(grant, config(), None, ledger=ledger)
        second = transactions.execute(grant, config(), None, ledger=ledger)
    return first, second


def test_signed_fixed_ids_use_one_write_and_replay_without_reads(notifications, tmp_path):
    grant = prepare()
    notifications["items"].append({"id": 104, "title": "new", "isRead": False})
    first, second = execute_twice(notifications, tmp_path, grant)
    assert first["receipts"]
    assert [{k: v for k, v in item.items() if k != "ts"} for item in first["receipts"]] == [
        {k: v for k, v in item.items() if k != "ts"} for item in second["receipts"]]
    assert [(request.method, request.url.path) for request in notifications["requests"]] == [
        ("GET", "/api/protected/notifications/state"),
        ("POST", "/api/protected/notifications/read")]
    posted = json.loads(notifications["requests"][-1].content)
    assert posted == {"ids": [101, 102], "all": False}
    assert notifications["items"][-1]["isRead"] is False


def test_already_read_targets_return_a_zero_change_fact_without_another_read(
        notifications, tmp_path):
    grant = prepare()
    for row in notifications["items"]:
        row["isRead"] = True
    first, second = execute_twice(notifications, tmp_path, grant)
    assert first["receipts"]
    assert [{k: v for k, v in item.items() if k != "ts"} for item in first["receipts"]] == [
        {k: v for k, v in item.items() if k != "ts"} for item in second["receipts"]]
    assert "本次标记 0 条" in first["reply"]
    assert first["receipts"][0]["change"] == "本来就读过"
    assert [request.method for request in notifications["requests"]] == ["GET", "POST"]


@pytest.mark.parametrize("fault", ["deleted", "foreign", "missing"])
def test_target_conflicts_are_rejected_before_changing_any_selected_row(
        notifications, tmp_path, fault):
    grant = prepare()
    second = next(row for row in notifications["items"] if row["id"] == 102)
    if fault == "deleted":
        second["deleted"] = True
    elif fault == "foreign":
        second["ownerUserId"] = 8
    else:
        notifications["items"].remove(second)
    before = copy.deepcopy(notifications["items"])
    first, replay = execute_twice(notifications, tmp_path, grant)
    assert first["receipts"] == replay["receipts"] == []
    assert notifications["items"] == before
    assert [request.method for request in notifications["requests"]] == ["GET", "POST"]


@pytest.mark.parametrize("override", [
    None, "success", {},
    {"ownerUserId": 8}, {"ownerUserId": True},
    {"scope": "all_unread"}, {"matchedIds": [101]}, {"matchedIds": [True, 102]},
    {"matchedIds": [102, 101]}, {"changedIds": [103]}, {"changedIds": [101, 101]},
    {"changedIds": [102, 101]}, {"isRead": False}, {"isRead": 1},
    {"unreadCount": None}, {"unreadCount": True}, {"unreadCount": -1},
])
def test_false_incomplete_or_lost_receipts_stay_unknown_and_cannot_be_replayed(
        notifications, tmp_path, override):
    grant = prepare()
    valid = {"scope": "selected", "ownerUserId": 7, "matchedIds": [101, 102],
             "changedIds": [101, 102], "isRead": True, "unreadCount": 0}
    notifications["receipt_override"] = (
        {**valid, **override} if isinstance(override, dict) and override else override)
    first, second = execute_twice(notifications, tmp_path, grant)
    assert first["blocked"] and second["blocked"]
    assert first["receipts"] == second["receipts"] == []
    assert "结果未知" in first["reply"] or "无法确认" in first["reply"]
    assert [request.method for request in notifications["requests"]] == ["GET", "POST"]
