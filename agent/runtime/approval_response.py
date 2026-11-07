"""Finalize the standard Agent's reply before its prepared approval is published."""

from langchain_core.messages import RemoveMessage

from agent.runtime.execution_state import capture
from agent.runtime.message_content import text_content
from agent.runtime.response_validation import incomplete_response
from agent.runtime.results import TurnResult
from utils.trace import record


def finish(session, response):
    reply = text_content(response.content)
    if not response.tool_calls and not incomplete_response(response) and reply.strip():
        session.outcome = TurnResult(reply, session.outcome.execution)
    elif not reply.strip() and not response.tool_calls and not response.invalid_tool_calls:
        attempts = session.state["proposal"].get("response_attempts", 0)
        if attempts == 0:
            session.state["proposal"]["response_attempts"] = 1
            record("native", "approval_response_retry", reason="empty")
            return {**capture(session), "jump_to": "model",
                    "messages": [RemoveMessage(id=response.id)] if response.id else []}
        record("native", "approval_response_failed", reason="empty")
    else:
        record("native", "approval_response_failed", reason="invalid_response")
    saved = {**capture(session), "jump_to": "end"}
    if incomplete_response(response) or response.tool_calls or response.invalid_tool_calls:
        saved["messages"] = [response.model_copy(update={
            "content": session.outcome.reply, "tool_calls": [], "invalid_tool_calls": [],
            "additional_kwargs": {}})]
    return saved
