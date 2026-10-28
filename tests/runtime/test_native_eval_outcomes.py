"""Equivalent task outcomes pass; missing proof, wrong dates and hidden writes fail."""

import json

import pytest

from eval import native_assertions, online_baseline


def case_gold(name):
    return next(c['gold'] for c in online_baseline.load_suite() if c['id'] == name)


def result(tool, data, **args):
    return {"native_events": [{"node": "native", "event": "call", "name": tool,
            "args": args, "kind": "ok", "result": json.dumps(data)}]}


@pytest.mark.parametrize("messages,expected", [(0, True), (1, False)])
def test_zero_unread_mail_is_a_proven_noop_only_with_an_actual_zero_count(messages, expected):
    gold = case_gold("messages_prepare")
    value = result("get_unread_counts", {"notifications": 3, "messages": messages,
                                          "total": 3 + messages})
    assert (not native_assertions.check(gold, value)) is expected


@pytest.mark.parametrize("items,more,expected", [([], False, True), ([], True, False),
                                               ([{"id": 1}], False, False)])
def test_only_an_empty_complete_inbox_proves_the_mail_read_request_is_already_done(
    items, more, expected
):
    value = result("list_my_messages", {"folder": "inbox", "items": items, "hasMore": more},
                   folder="inbox")
    assert (not native_assertions.check(case_gold("messages_prepare"), value)) is expected


def test_failed_or_malformed_empty_result_is_not_a_noop_and_writes_never_pass():
    value = result("get_unread_counts", {"messages": 0})
    value["native_events"][0]["kind"] = "unavailable"
    gold = case_gold("messages_prepare")
    assert native_assertions.check(gold, value)
    value["native_events"][0].update(kind="ok", result="not-json")
    assert native_assertions.check(gold, value)
    value = result("get_unread_counts", {"messages": 0})
    value["native_events"].append({"event": "write_done"})
    assert native_assertions.check(gold, value)


@pytest.mark.parametrize("text,due,expected", [("交房租", "2026-10-27", True),
    ("交房租", "2026-10-26", False), ("明天交房租", None, False), ("别的事", "2026-10-27", False)])
def test_todo_checks_content_and_real_calendar_due_date_in_the_same_confirmation(
    tmp_path, text, due, expected
):
    path = tmp_path / "trace.json"
    path.write_text(json.dumps({"started_at": "2026-10-26T23:59:00+08:00"}))
    value = {"trace": str(path), "native_events": [], "confirm_payloads": [{"specs": [
        {"tool": "create_dashboard_todo", "args": {"text": text, "date": due}}]}]}
    gold = {"require_todo_schedule": {"text": "交房租", "days": 1}}
    assert (not native_assertions.check(gold, value)) is expected


def test_relative_schedule_without_request_time_evidence_cannot_pass():
    assert native_assertions.check({"require_todo_schedule": {"text": "交房租", "days": 1}},
                                   {"native_events": []})
