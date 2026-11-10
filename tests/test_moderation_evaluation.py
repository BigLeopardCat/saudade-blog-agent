"""A complete zero pending count proves an empty list; other counts do not."""

import json

import pytest

from eval import native_assertions, online_baseline


@pytest.mark.parametrize("scope,pending,kind,passed", [
    ("not_deleted", 0, "ok", True),
    ("not_deleted", 1, "ok", False),
    ("not_deleted", None, "ok", False),
    ("not_deleted", False, "ok", False),
    ("not_deleted", "0", "ok", False),
    ("page", 0, "ok", False),
    (None, 0, "ok", False),
    ("not_deleted", 0, "unavailable", False),
])
def test_pending_comments_require_items_unless_summary_proves_empty(scope, pending, kind, passed):
    case = next(c for c in online_baseline.load_suite() if c["id"] == "admin_comments")
    evidence = {"comments": {"scope": scope, "finalStatus": {"pending": pending}}}
    result = {"native_events": [{"node": "native", "event": "call",
              "name": "get_moderation_status", "kind": kind,
              "result": json.dumps(evidence)}]}
    assert (not native_assertions.check(case["gold"], result)) is passed
    result["native_events"].append({"node": "native", "event": "call",
        "name": "list_admin_comments", "kind": "ok", "result": '{"items": []}'})
    assert not native_assertions.check(case["gold"], result)


def test_other_resource_zero_and_absent_observations_do_not_prove_empty_comments():
    case = next(c for c in online_baseline.load_suite() if c["id"] == "admin_comments")
    result = {"native_events": [{"node": "native", "event": "call",
              "name": "get_moderation_status", "kind": "ok",
              "result": '{"board":{"scope":"all","finalStatus":{"pending":0}}}'}]}
    assert native_assertions.check(case["gold"], result)
    assert native_assertions.check(case["gold"], {"native_events": []})
