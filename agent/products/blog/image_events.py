"""Turn verified host image completions into one framework tool exchange and report."""

import hashlib

from langchain_core.messages import AIMessage

from agent.domains.image_generation.tools import read
from agent.principal import ADMIN_ROLES
from agent.runtime.memory import bind_actor
from agent.runtime.results import TurnResult
from agent.runtime.writes import bound_context
from tools import base


def prepare(event, snapshot, config):
    principal, cid = bound_context(config)
    if event.get("kind") != "image_generation" or principal.known_role not in ADMIN_ROLES:
        raise ValueError("image_event_forbidden")
    event_id = "image_" + hashlib.sha256(
        f"{event['job_id']}:{event['version']}".encode()).hexdigest()
    if config["configurable"].get("turn_id") != event_id:
        raise ValueError("image_event_identity_mismatch")
    for message in snapshot.messages:
        if message.id == "result:" + event_id and isinstance(message, AIMessage):
            execution = ({**snapshot.pending, "task_id": event_id}
                         if snapshot.run and snapshot.run["id"] == event_id else None)
            return TurnResult(str(message.content), execution)
    job = read(event["job_id"], config)
    if (isinstance(job, base.ToolResult) or job.conversationId != cid
            or job.eventVersion != event["version"]
            or job.status not in {"review", "accepted", "failed", "unknown"}):
        raise ValueError("image_event_deferred")
    name = ("prepare_generated_image_acceptance" if job.status == "review"
            else "get_image_generation")
    args = {"job_id": job.id}
    if job.status == "review":
        args["storage"] = job.saveStorage
    message = AIMessage(content="", id="event:" + event_id, tool_calls=[{
        "type": "tool_call", "id": event_id, "name": name, "args": args}],
        additional_kwargs={"runtime_event": {"kind": "image_generation", "id": event_id}})
    return bind_actor([message], principal)[0]
