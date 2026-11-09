"""Conversation acceptance checks outcomes and scope instead of forcing tool paths."""

import json

import pytest

from eval.native_assertions import check
from eval.online_baseline import load_suite

FACT = {"status": "superseded", "scope": "operation", "executed": False,
        "business_state": "not_observed"}
FEEDBACK = {"node": "native", "event": "approval_feedback", "facts": FACT}
PHASE = {"node": "native", "event": "turn_state", "phase": "cancelled", "receipt_count": 0}


@pytest.mark.parametrize("events,passed", [
    ([FEEDBACK, PHASE], True),
    ([PHASE], False),
    ([FEEDBACK], False),
    ([{**FEEDBACK, "node": "untrusted"}, PHASE], False),
    ([{**FEEDBACK, "facts": {**FACT, "executed": True}}, PHASE], False),
    ([{**FEEDBACK, "facts": {**FACT, "scope": "entity"}}, PHASE], False),
    ([FEEDBACK, {**PHASE, "receipt_count": 1}], False),
    ([FEEDBACK, PHASE, {**PHASE, "phase": "awaiting_confirmation"}], False),
    ([{**FEEDBACK, "event": "operation_observation", "call_id": "orphan"}, PHASE], False),
])
def test_revocation_needs_scoped_facts_and_a_terminal_cancelled_state(events, passed):
    assert bool(check({"require_revoked_operation": True}, {"native_events": events})) is not passed


def test_explicit_cancel_tool_can_prove_the_same_outcome():
    events = [{"node": "native", "event": "call", "name": "cancel_pending_operation",
               "kind": "cancelled", "call_id": "cancel", "round": 1},
              {**FEEDBACK, "event": "operation_observation", "call_id": "cancel", "round": 1},
              PHASE]
    assert not check({"require_revoked_operation": True}, {"native_events": events})


@pytest.mark.parametrize("returned_cid,args,passed", [
    (42, {"limit": 10}, True),
    (43, {"limit": 10}, False),
    (True, {}, False),
    (42, {"conversation_id": 43}, False),
])
def test_optional_history_read_must_match_the_request_conversation(
    tmp_path, returned_cid, args, passed
):
    trace = tmp_path / "trace.json"
    trace.write_text(json.dumps({"input": {"conversation_id": 42}}))
    result = {"trace": str(trace), "native_events": [{
        "node": "native", "event": "call", "name": "get_chat_history", "kind": "ok",
        "args": args, "result": json.dumps({"conversationId": returned_cid, "items": []})}]}
    assert bool(check({"require_current_conversation_history": True}, result)) is not passed


@pytest.mark.parametrize("name", ["get_chat_history", "list_accounts", "rag_search"])
def test_missing_coordinates_or_unrelated_reads_cannot_pass_history_isolation(name):
    events = [{"node": "native", "event": "call", "name": name, "kind": "ok",
               "result": '{"conversationId":42,"items":[]}'}]
    assert check({"require_current_conversation_history": True}, {"native_events": events})


def test_answering_from_current_context_does_not_require_a_history_lookup():
    assert not check({"require_current_conversation_history": True}, {"native_events": []})
    assert check({"require_current_conversation_history": True}, {})


def test_online_cases_use_result_and_scope_contracts():
    cases = {case["id"]: case for case in load_suite()}
    assert cases["pending_cancel"]["rounds"][1]["gold"]["require_revoked_operation"]
    assert cases["isolation_new_session"]["gold"]["require_current_conversation_history"]
    assert cases["isolation_new_session"]["gold"]["text_not_contains"] == ["薄荷星云"]


def test_composite_cancellation_accepts_text_only_with_proven_revocation():
    cases = {case["id"]: case for case in load_suite("eval/golden/native_requests.jsonl")}
    case = cases["notice_then_role_cancel"]
    assert case["decision_expectations"][1]["allow_text"] is True
    gold = case["rounds"][1]["gold"]
    assert gold["require_revoked_operation"] is True
    assert check(gold, {"native_events": []})
    assert not check(gold, {"native_events": [FEEDBACK, PHASE]})
