"""Inject user steering through standard Agent message updates at model boundaries."""

from langchain_core.messages import ToolMessage

from agent.runtime.task_inputs import TaskInputEvent, consumed, messages
from utils.trace import record


def additions(session, state, *, response=None):
    if not session.inputs_active:
        return []
    task = session.task
    accepted = consumed(state, task["id"])
    session.input_messages = accepted
    seen = {message.additional_kwargs["task_input"]["id"] for message in accepted}
    if response is not None:
        session.applied_inputs.update(seen)
    pending = session.input_inbox.boundary(task, seen,
        final=response is not None and not response.tool_calls, applied=session.applied_inputs)
    if not pending:
        return []
    appended = []
    if response is not None:
        # Complete the provider transcript without executing calls proposed
        # before the user's new requirements arrived.
        appended.extend(ToolMessage(content="用户补充了要求，本次调用尚未执行。",
                                   tool_call_id=call["id"], name=call["name"],
                                   artifact={"phase": "superseded"})
                        for call in response.tool_calls)
    appended.extend(messages(pending, task["id"]))
    record("native", "task_input_consumed", task_id=task["id"],
           input_ids=[item["id"] for item in pending])
    return appended


def events(session, data, reported):
    for update in data.values():
        if not isinstance(update, dict) or not session.task:
            continue
        task_id = session.task["id"]
        known = {message.id for message in session.input_messages}
        session.input_messages.extend(message for message in consumed(update, task_id)
                                      if message.id not in known)
        applied = []
        for message in session.input_messages:
            item_id = message.additional_kwargs["task_input"]["id"]
            if item_id in update.get("applied_inputs", ()):
                applied.append({"id": item_id, "text": message.content, "status": "applied"})
        if any(item["id"] not in reported for item in applied):
            reported.update(item["id"] for item in applied)
            yield TaskInputEvent({"task_id": task_id, "inputs": applied})


def replace_unpublished_proposal(session, state):
    execution = session.outcome.execution if session.outcome else {}
    if not (execution or {}).get("pending_confirm"):
        return []
    extra = additions(session, state)
    if not extra:
        return []
    replaced = [message.model_copy(update={
        "content": "用户补充了要求，原操作未执行，将按新要求重新处理。",
        "artifact": {"phase": "superseded"}})
        for message in state.get("messages", []) if isinstance(message, ToolMessage)
        and (message.artifact or {}).get("phase") in {"awaiting_approval", "deferred"}]
    # No confirmation has left the runtime yet. The old proposal is never
    # published or persisted as the conversation's authorized pending action.
    session.outcome, session.awaiting_call, session.approval = None, None, None
    return [*replaced, *extra]
