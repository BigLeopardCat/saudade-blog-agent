"""A terminal reply and optional existing execution-protocol updates."""

import json
import uuid
from dataclasses import dataclass

from langchain_core.messages import AIMessage, ToolMessage

from agent.runtime.memory import observation
from tools.result import ok


@dataclass(frozen=True)
class ReplyReset:
    """Withdraw this turn's provisional prose, retaining all execution facts."""


@dataclass(frozen=True)
class TurnResult:
    reply: str
    execution: dict | None = None



def turn_phase(result):
    execution = result.execution or {}
    return ("paused" if execution.get("paused_task") else
            "awaiting_browser" if execution.get("client_action") else
            "awaiting_confirmation" if execution.get("pending_confirm") else
            "blocked" if execution.get("blocked") else
            "cancelled" if (execution.get("pending_event") or {}).get("event") == "cancelled"
            else "responded")


def complete_reply(result, displayed):
    """Merge observed prose with a terminal outcome for storage and transport."""
    reply = str(result.reply or "")
    if displayed and reply.startswith(displayed):
        missing = reply[len(displayed):]
    elif displayed and reply and displayed.endswith(reply):
        missing = ""
    elif displayed and reply:
        missing = ("\n" if not displayed.endswith("\n") else "") + reply
    else:
        missing = reply
    execution = result.execution
    terminal_key = ("confirm_text" if execution and execution.get("pending_confirm") else
                    "noop_text" if execution and execution.get("noop_note") else None)
    if terminal_key:
        execution = {**execution, terminal_key: missing}
    return TurnResult(displayed + missing, execution), missing, terminal_key


def receipt_observations(result, *, user_confirmed=False):
    """Keep verified facts in memory even when conversational wording is model-generated."""
    messages = []
    approval = {"approval": "user_confirmed"} if user_confirmed else {}
    if user_confirmed and (result.execution or {}).get("approval_selection"):
        approval["selection"] = result.execution["approval_selection"]
    for row in (result.execution or {}).get("receipts", ()):
        call = {
            "name": row["tool"],
            "args": row.get("args") or {},
            "id": "executed_" + uuid.uuid4().hex,
        }
        fact = json.dumps(
            {**approval, "status": row.get("status", "executed"), "result": row.get("result"),
             "action": row.get("action")}, ensure_ascii=False
        )
        messages.extend(observation(call, ok(fact)))
    outcome = (result.execution or {}).get("operation_outcome")
    if outcome:
        call = {"name": outcome["tool"], "args": outcome["args"],
                "id": "unverified_" + uuid.uuid4().hex}
        messages.extend([AIMessage(content="", tool_calls=[call]), ToolMessage(
            content=json.dumps({**approval, "status": "unverified", "scope": "operation",
                                "result": outcome["result"]}, ensure_ascii=False),
            tool_call_id=call["id"], name=call["name"], status="error")])
    return messages


def approval_observations(result):
    messages = receipt_observations(result, user_confirmed=True)
    authorized = (result.execution or {}).get("authorized_operation")
    if authorized:
        call = {"name": authorized["tool"], "args": authorized["args"],
                "id": "approved_" + authorized["operation_id"]}
        messages.extend(observation(call, ok(json.dumps({"status": "approved",
            "approval": "user_confirmed", "executed": False,
            "result": "已获用户确认，尚未应用；以编辑器执行回执为准。"}, ensure_ascii=False))))
    return messages


def pause_notice(result):
    notice = "完成后继续剩余请求；取消将停止后续处理。"
    execution = result.execution
    popup = execution["pending_confirm"]
    action = execution["pending_action"]
    return TurnResult(result.reply + "\n" + notice, {
        **execution, "confirm_text": execution.get("confirm_text", result.reply) + "\n" + notice,
        "pending_confirm": {**popup, "q": popup["q"] + "\n" + notice},
        "pending_action": {**action, "question": action["question"] + "\n" + notice},
    })


def cancellation_fact():
    """Revocation proves this operation did not execute, not current entity state."""
    return {"status": "cancelled", "scope": "operation", "executed": False,
            "business_state": "not_observed"}


def cancellation_content(reply):
    return json.dumps({**cancellation_fact(), "result": reply}, ensure_ascii=False)
