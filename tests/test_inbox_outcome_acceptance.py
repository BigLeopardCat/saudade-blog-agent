"""Unread overview headers are evidence; counts alone and wrong scopes are not."""

import copy
import json

import pytest

from eval.golden_evaluation import check_gold
from eval.online_baseline import load_suite


def overview():
    return {"notifications": 1, "messages": 0, "total": 1, "limit": 5,
        "unreadOnly": True, "itemsIncluded": True, "notificationsHasMore": False,
        "messagesHasMore": False, "messageItems": [], "notificationItems": [{
            "id": 9, "title": "A notice", "titleTruncated": False,
            "createdAt": "2026-11-15T01:00:00", "isRead": False, "type": "notice"}]}


@pytest.mark.parametrize("change,valid", [({}, True),
    ({"itemsIncluded": False}, False), ({"notificationItems": []}, False),
    ({"notifications": 2}, False), ({"unreadOnly": False}, False),
    ({"notifications": 0, "total": 0, "notificationItems": []}, True)])
def test_combined_task_requires_verified_items_or_a_consistent_empty_overview(change, valid):
    case = next(c for c in load_suite() if c["id"] == "tail_multi_own")
    data = {**copy.deepcopy(overview()), **change}
    calls = [{"node": "native", "event": "call", "name": name, "kind": "ok",
              "args": {}, "result": json.dumps(value)} for name, value in [
                  ("get_inbox_overview", data), ("list_my_favorites", [])]]
    result = {"text": "Unread notices and favorites checked.", "tool_calls": [],
              "commands": [], "exec_rows": [], "native_events": calls}
    assert (not check_gold(case["gold"], result)) is valid
