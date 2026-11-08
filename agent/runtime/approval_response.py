"""Finalize the standard Agent's reply before its prepared approval is published."""

from langchain_core.messages import HumanMessage, RemoveMessage

from agent.runtime.execution_state import capture
from agent.runtime.message_content import text_content
from agent.runtime.response_validation import incomplete_response
from agent.runtime.results import TurnResult
from utils.trace import record


def finish(session, response):
    reply = text_content(response.content)
    issue = incomplete_response(response)
    if not response.tool_calls and not issue and reply.strip():
        session.outcome = TurnResult(reply, session.outcome.execution)
    elif not response.tool_calls and (
            not issue and not reply.strip() or issue and issue[0] == "invalid_tool_call"):
        attempts = session.state["proposal"].get("response_attempts", 0)
        if attempts == 0:
            session.state["proposal"]["response_attempts"] = 1
            reason = issue[0] if issue else "empty"
            record("native", "approval_response_retry", reason=reason)
            messages = [RemoveMessage(id=response.id)] if response.id else []
            if issue:
                messages.append(HumanMessage(content=
                    "Runtime protocol feedback: the previous reply was malformed and discarded. "
                    "The existing confirmation is still pending; no further action is authorized. "
                    "Provide only the requested user-facing answer, including any requested "
                    "summary from recorded read results. Do not call tools or emit control syntax.",
                    additional_kwargs={"runtime_feedback": "approval_response_repair"}))
            return {**capture(session), "jump_to": "model", "messages": messages}
        record("native", "approval_response_failed", reason=issue[0] if issue else "empty")
    else:
        record("native", "approval_response_failed", reason="invalid_response")
    saved = {**capture(session), "jump_to": "end"}
    if incomplete_response(response) or response.tool_calls or response.invalid_tool_calls:
        saved["messages"] = [response.model_copy(update={
            "content": session.outcome.reply, "tool_calls": [], "invalid_tool_calls": [],
            "additional_kwargs": {}})]
    return saved
