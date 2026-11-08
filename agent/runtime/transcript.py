"""Commit the framework message sequence without rebuilding provider exchanges."""

import json

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from agent.runtime import memory
from agent.runtime.memory_media import compact
from agent.runtime.message_content import text_content
from agent.runtime.response_validation import incomplete_response


def _close_exchange(pending):
    return [ToolMessage(tool_call_id=call["id"], name=call["name"], status="error",
        content=json.dumps({"status": "unverified", "reason": "no_recorded_result",
                            "guidance": "Do not infer success or retry an effect from this gap."}),
        artifact={"kind": "unavailable"}) for call in pending.values()]


def finish(messages, reply, principal, accepted):
    """Keep wire content, grouping and replay fields; remove transient authority artifacts."""
    accepted_ids = {message.id for message in accepted if message.id
                    and memory.is_user_input(message)}
    saved, pending = [], {}
    for message in messages:
        if memory.is_context(message):
            continue
        if (isinstance(message, HumanMessage) and message.additional_kwargs.get("task_input")
                and message.id not in accepted_ids):
            continue
        if not isinstance(message, (HumanMessage, AIMessage, ToolMessage)):
            continue
        if isinstance(message, (HumanMessage, AIMessage)) and pending:
            saved.extend(_close_exchange(pending))
            pending.clear()
        message = compact(message)
        if isinstance(message, HumanMessage) and message.id in accepted_ids:
            message = memory.bind_actor([message], principal)[0]
        if isinstance(message, AIMessage):
            if incomplete_response(message):
                # Malformed control text is never promoted into conversation memory.
                message = message.model_copy(update={
                    "content": "Invalid model output was discarded.",
                    "tool_calls": [], "invalid_tool_calls": [], "additional_kwargs": {}})
            pending = {call["id"]: call for call in message.tool_calls}
        elif isinstance(message, ToolMessage):
            if message.tool_call_id not in pending:
                continue
            pending.pop(message.tool_call_id)
            artifact = message.artifact if isinstance(message.artifact, dict) else {}
            message = message.model_copy(update={"artifact": {
                key: artifact[key] for key in ("kind", "data", "meta", "image_reader",
                                               "article_evidence", "receipts") if key in artifact}})
        saved.append(message)
    saved.extend(_close_exchange(pending))
    last = saved[-1] if saved else None
    if not (isinstance(last, AIMessage) and not last.tool_calls and last.content
            and reply.endswith(text_content(last.content))):
        saved.append(AIMessage(content=reply))
    return saved
