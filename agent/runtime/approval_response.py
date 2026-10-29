"""Finalize the standard Agent's reply before its prepared approval is published."""

from agent.runtime.execution_state import capture
from agent.runtime.message_content import text_content
from agent.runtime.response_validation import incomplete_response
from agent.runtime.results import TurnResult
from utils.trace import record


def finish(session, response):
    reply = text_content(response.content).strip()
    if not response.tool_calls and not incomplete_response(response) and reply:
        session.outcome = TurnResult(reply, session.outcome.execution)
    elif not reply and not response.tool_calls and not response.invalid_tool_calls:
        attempts = session.state["proposal"].get("response_attempts", 0)
        if attempts == 0:
            session.state["proposal"]["response_attempts"] = 1
            record("native", "approval_response_retry", reason="empty")
            return {**capture(session), "jump_to": "model"}
        record("native", "approval_response_failed", reason="empty")
    else:
        record("native", "approval_response_failed", reason="invalid_response")
    return {**capture(session), "jump_to": "end"}
