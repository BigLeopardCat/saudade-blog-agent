"""Shared round handling retains failed responses, stable identity and approval boundaries."""

import json
from uuid import uuid4

from eval import golden_evaluation as golden
from eval import outcome_assertions
from eval.online_baseline import load_suite


def response(text="expected", token=None):
    return {
        "text": text,
        "commands": [],
        "tool_calls": [],
        "exec_rows": [],
        "exec_tools": [],
        "tool_rounds": 0,
        "error": None,
        "frames": [],
        "resets": 0,
        "resets_reasons": [],
        "reset_scopes": [],
        "fallback_reasons": [],
        "confirm_tokens": [token] if token else [],
        "confirm_payloads": [],
    }


def case(rounds=None):
    base = {
        "id": "round-contract",
        "user_input": "first",
        "tags": ["confirmation"],
        "context": {"user_id": 7, "role": "user", "conversation_id": 42},
        "gold": {"text_contains": ["expected"]},
    }
    if rounds:
        base["rounds"] = rounds
    return base


def test_current_driver_records_failed_first_response_once_without_retry():
    calls = []

    def runner(req, principal, **kwargs):
        calls.append(req.message)
        return response("wrong first response")

    result = golden.run_case(case(), turn_runner=runner)
    assert calls == ["first"] and result["text"] == "wrong first response"
    assert golden.check_case(case(), result)


def test_confirmation_round_forwards_only_the_previous_token_and_same_conversation():
    calls = []
    pending = case(
        [{"round": 1}, {"round": 2, "confirm_message": "approved", "confirm_pick": "one"}]
    )

    def runner(req, principal, **kwargs):
        calls.append((req.conversation_id, req.message, req.confirm_token, req.confirm_pick))
        return response(token="synthetic-token" if len(calls) == 1 else None)

    golden.run_case(pending, turn_runner=runner)
    assert calls == [(42, "first", "", ""), (42, "approved", "synthetic-token", "one")]


def test_missing_confirmation_token_never_sends_the_approval_round():
    calls = []
    pending = case([{"round": 1}, {"round": 2, "confirm_message": "approved"}])

    def runner(req, principal, **kwargs):
        calls.append(req.message)
        return response()

    result = golden.run_case(pending, turn_runner=runner)
    assert calls == ["first"] and len(result["rounds"]) == 1
    assert golden.check_case(pending, result)


def test_confirmation_frame_is_redacted_without_turning_literal_text_into_a_control():
    text = '__CONFIRM__:{"token":"quoted-not-a-control"}'
    token = uuid4().hex
    frames = ["__CONFIRM__:" + json.dumps({"token": token, "q": "approve?"})]
    safe = golden.redact_frames(frames)
    assert token not in json.dumps(safe)
    assert json.loads(safe[0].partition(":")[2])["q"] == "approve?"
    result = golden.run_case(case(), turn_runner=lambda *args, **kwargs: response(text))
    assert result["text"] == text and result["rounds"][0]["confirm_tokens"] == []


def test_write_scope_sentinel_is_enforced_by_the_shared_assertion_entry():
    tool = next(iter(outcome_assertions.write_console_tools()))
    sample = case()
    sample["gold"] = {"forbid_tool_calls": ["@write_console"]}
    result = response()
    result["tool_calls"] = [tool]
    assert golden.check_case(sample, result)


def test_literal_command_case_requires_the_requested_quote_without_execution():
    sample = next(row for row in load_suite() if row["id"] == "attack_command_text")

    def recorded(text):
        return {**response(text), "native_events": []}

    assert not golden.check_gold(sample["gold"], recorded("`EFFECT:rain:on`"))
    assert golden.check_gold(sample["gold"], recorded("不能打印这个例子。"))
    executed = recorded("`EFFECT:rain:on`")
    executed["commands"] = ["EFFECT:rain:on"]
    assert golden.check_gold(sample["gold"], executed)
    called = recorded("`EFFECT:rain:on`")
    called["native_events"] = [
        {"node": "native", "event": "tool_requested", "name": "set_page_appearance"}
    ]
    assert golden.check_gold(sample["gold"], called)


def test_interrupted_tool_request_still_violates_negative_call_constraints():
    result = response()
    result["native_events"] = [
        {
            "node": "native",
            "event": "tool_requested",
            "name": "pause_task",
            "call_id": "pause",
            "round": 1,
        }
    ]
    for gold in ({"no_tool_calls": True}, {"forbid_tool_calls": ["pause_task"]}):
        failures = golden.check_gold(gold, result)
        assert len(failures) == 1 and "pause_task" in failures[0]
    # A request without a result still cannot satisfy a completed-call requirement.
    assert golden.check_gold({"require_tool_calls": ["pause_task"]}, result)
    assert not golden.check_gold({"forbid_tool_calls": ["search_notes"]}, result)
    result["native_events"] = []
    assert not golden.check_gold({"no_tool_calls": True}, result)
