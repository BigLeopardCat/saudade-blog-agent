"""A terminal reply and optional existing execution-protocol updates."""

import json
import uuid
from dataclasses import dataclass

from agent.runtime.memory import observation
from tools.result import ok


@dataclass(frozen=True)
class TurnResult:
    reply: str
    execution: dict | None = None


def receipt_observations(result):
    """Keep verified facts in memory even when conversational wording is model-generated."""
    messages = []
    for row in (result.execution or {}).get("receipts", ()):
        call = {
            "name": row["tool"],
            "args": row.get("args") or {},
            "id": "executed_" + uuid.uuid4().hex,
        }
        fact = json.dumps(
            {"result": row.get("result"), "action": row.get("action")}, ensure_ascii=False
        )
        messages.extend(observation(call, ok(fact)))
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
