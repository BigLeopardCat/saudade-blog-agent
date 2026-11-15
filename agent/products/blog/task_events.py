"""Report an owner-bound completed workflow through the original checkpoint."""

import hashlib

from langchain_core.messages import AIMessage

from agent.principal import ADMIN_ROLES
from agent.runtime.memory import bind_actor
from agent.runtime.results import TurnResult
from agent.runtime.writes import bound_context
from tools import base


def prepare(event, snapshot, config, scheduling):
    principal, cid = bound_context(config)
    if principal.known_role not in ADMIN_ROLES or scheduling is None:
        raise ValueError("task_event_forbidden")
    event_id = "task_" + hashlib.sha256(
        f"{event['run_id']}:{event['version']}".encode()).hexdigest()
    if config["configurable"].get("turn_id") != event_id:
        raise ValueError("task_event_identity_mismatch")
    for message in snapshot.messages:
        if message.id == "result:" + event_id and isinstance(message, AIMessage):
            return TurnResult(str(message.content))
    row = base._admin_get(
        f"/api/protected/automation-results/{event['run_id']}?conversation_id={cid}", config)
    if (not isinstance(row, dict) or row.get("conversationId") != cid
            or row.get("runId") != event["run_id"]
            or row.get("eventVersion") != event["version"]):
        raise ValueError("task_event_deferred")
    schedule = row.get("scheduleId")
    if row.get("status") not in {"completed", "incomplete", "failed", "unknown"}:
        raise ValueError("task_event_deferred")
    message = AIMessage(content="", id="event:" + event_id, tool_calls=[{
        "type": "tool_call", "id": event_id, "name": "get_automation_result",
        "args": {"schedule_id": schedule, "run_id": event["run_id"]}}],
        additional_kwargs={"runtime_event": {"kind": "task_result", "id": event_id}})
    return bind_actor([message], principal)[0]
