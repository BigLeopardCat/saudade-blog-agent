"""Bind explicit task continuation to the framework's saved execution state."""

import uuid

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from agent.runtime import approval, memory
from agent.runtime.execution_state import restore
from agent.runtime.memory_media import compact
from agent.runtime.message_content import text_content


def new_run(config, messages, task_id=None):
    try:
        scope = approval.binding(config)
    except ValueError:
        return None
    return {"id": task_id or uuid.uuid4().hex, "scope": scope,
            "input": [compact(message) for message in messages], "input_saved": False}


def allowed(snapshot, task_id, config):
    run = snapshot.run
    try:
        return bool(snapshot.unfinished and not snapshot.has_pending and run
                    and run["id"] == task_id and run["scope"] == approval.binding(config))
    except (KeyError, ValueError):
        return False


def restore_session(session, state):
    """Recover metadata and successful observations; never infer write permission."""
    restore(session, state)
    messages = state.get("messages") or []
    session._restore_domains(messages)
    start = max((i for i, message in enumerate(messages) if isinstance(message, HumanMessage)
                 and not memory.is_context(message)), default=0)
    calls = {}
    result_chars = 0
    for message in messages[start:]:
        if isinstance(message, AIMessage):
            calls.update({call["id"]: call for call in message.tool_calls})
        elif isinstance(message, ToolMessage):
            artifact = message.artifact or {}
            if "kind" in artifact and not artifact.get("phase"):
                result_chars += len(text_content(message.content))
            session.receipts.extend(artifact.get("receipts") or [])
            if artifact.get("phase") == "awaiting_approval" and artifact.get("proposal"):
                restore(session, {**state, "proposal": artifact["proposal"],
                                  "awaiting_call": message.tool_call_id})
            if message.tool_call_id in calls:
                session.observations.extend(memory.observation(
                    calls[message.tool_call_id], message))
    session.result_chars = max(session.result_chars, result_chars)
    # A completed child may precede the interrupted parent commit.
    last = messages[-1] if messages else None
    if session.outcome is None and isinstance(last, AIMessage) and not last.tool_calls:
        reply = text_content(last.content)
        if reply:
            session.outcome = session._result(reply)


def discard_interrupted(runtime, config, values, text):
    run = values.get("run")
    if not run or values.get("approval") or not text.strip():
        return None
    from agent.checkpoint import _same_turn, _user_text_of

    target = next((m for m in reversed(run["input"])
                   if isinstance(m, HumanMessage) and not memory.is_context(m)), None)
    if target is None or not _same_turn(_user_text_of(target), text):
        return {"dropped": False, "why": "mismatch"}
    runtime.update_state(config, {"run": None})
    gone = not runtime.get_state(config).values.get("run")
    return {"dropped": gone, "why": "" if gone else "still_present"}


def new_observations(snapshot, observations):
    seen = {m.tool_call_id for m in snapshot.messages if isinstance(m, ToolMessage)}
    return [m for m in observations
            if not (isinstance(m, ToolMessage) and m.tool_call_id in seen)
            and not (isinstance(m, AIMessage) and m.tool_calls
                     and all(c["id"] in seen for c in m.tool_calls))]
