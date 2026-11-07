"""Bind explicit task continuation to the framework's saved execution state."""

import uuid

from langchain_core.messages import AIMessage, ToolMessage

from agent.runtime import memory, task_scope
from agent.runtime.execution_state import restore
from agent.runtime.memory_media import compact
from agent.runtime.message_content import text_content


def new_run(config, messages, task_id=None):
    try:
        scope = task_scope.binding(config)
    except ValueError:
        return None
    return {"id": task_id or uuid.uuid4().hex, "scope": scope,
            "input": [compact(message) for message in messages], "input_saved": False}


def allowed(snapshot, task_id, config):
    run = snapshot.run
    try:
        return bool(snapshot.unfinished and not snapshot.has_pending and run
                    and run["id"] == task_id and run["scope"] == task_scope.binding(config))
    except (KeyError, ValueError):
        return False


def input_context(snapshot, config):
    """Preserve accepted user text independently of permission to resume execution."""
    run = snapshot.run
    try:
        if (not snapshot.unfinished or not run or run.get("input_saved")
                or run["scope"] != task_scope.binding(config)):
            return []
    except (KeyError, ValueError):
        return []
    principal = config["configurable"]["principal"]
    # Canonical turn IDs belong to accepted inputs; imported host history and
    # unfinished tool exchanges cannot acquire an actor or execution authority here.
    messages = [message for message in run.get("input", [])
                if memory.is_user_input(message) and memory.latest_turn_id([message])]
    return memory.bind_actor(messages, principal)


def restore_session(session, state):
    """Recover metadata and successful observations; never infer write permission."""
    restore(session, state)
    if session.task:
        from agent.runtime.task_inputs import consumed
        session.input_messages = consumed(state, session.task["id"])
    messages = state.get("messages") or []
    start = max((i for i, message in enumerate(messages) if memory.is_user_input(message)
                 and not message.additional_kwargs.get("task_input")), default=0)
    calls = {}
    effects = {}
    for message in messages[start:]:
        if isinstance(message, AIMessage):
            calls.update({call["id"]: call for call in message.tool_calls})
        elif isinstance(message, ToolMessage):
            artifact = message.artifact or {}
            effects.update(artifact.get("effects") or {})
            session.receipts.extend(artifact.get("receipts") or [])
            if artifact.get("phase") == "awaiting_approval" and artifact.get("proposal"):
                restore(session, {**state, "proposal": artifact["proposal"],
                                  "awaiting_call": message.tool_call_id})
            if message.tool_call_id in calls:
                session.observations.extend(memory.observation(
                    calls[message.tool_call_id], message))
    session.state["effects"] = effects | session.state["effects"]
    # A completed child may precede the interrupted parent commit.
    last = messages[-1] if messages else None
    if session.outcome is None and isinstance(last, AIMessage) and not last.tool_calls:
        reply = text_content(last.content)
        if reply:
            session.outcome = session._result(reply)
