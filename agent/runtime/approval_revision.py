"""Replace a prepared approval through native checkpoint reducers without a model call."""

from copy import deepcopy

from langchain_core.messages import ToolMessage

from agent import confirm
from agent.runtime import approval
from agent.runtime.approval_messages import prepared


def replace(runtime, config, grant, result, *, digest):
    """The caller holds the conversation lock and has durably revoked the old grant."""
    snapshot = runtime.store.load(config)
    if not approval.matches(snapshot.approval, grant, config):
        raise ValueError("approval_changed")
    execution = result.execution
    new_grant = confirm.verify(execution["pending_confirm"]["token"], grant["uid"], grant["conv"])
    if new_grant is None:
        raise ValueError("invalid_revised_approval")
    origin = (snapshot.approval.get("revision") or {}).get("previous_jti", grant["jti"])
    revised = {"previous_jti": origin, "proposal": {"reply": result.reply, "execution": execution}}
    binding = {
        "jti": new_grant["jti"],
        "scope": approval.binding(config),
        "revision": revised,
        "request_jti": grant["jti"],
        "draft_digest": digest,
    }
    messages = []
    for message in snapshot.messages:
        prior = (getattr(message, "artifact", None) or {}).get("proposal", {})
        if (
            isinstance(message, ToolMessage)
            and prior.get("execution", {}).get("pending_action", {}).get("jti") == grant["jti"]
        ):
            updated = prepared(
                {"name": message.name, "id": message.tool_call_id}, revised["proposal"], []
            )
            message = updated.model_copy(update={"id": message.id})
        messages.append(message)
    runtime.store.update_state(
        config,
        {
            "messages": messages,
            "pending_confirm": execution["pending_confirm"],
            "pending_action": execution["pending_action"],
            "approval": binding,
            "approved_result": None,
        },
    )
    # Updating as commit schedules only the standard approval interrupt, never the Agent.
    approval.graph(runtime.saver).invoke(None, config)
    return execution


def apply(session, state, facts):
    """Carry the human's saved revision into the resumed native tool exchange."""
    revision = facts.get("proposal_revision")
    if not revision:
        return state
    original = (session.outcome.execution or {}) if session.outcome else {}
    if original.get("pending_action", {}).get("jti") != revision["previous_jti"]:
        raise ValueError("approval_revision_mismatch")
    proposal = deepcopy(revision["proposal"])
    if proposal["execution"]["pending_action"]["jti"] != facts["jti"]:
        raise ValueError("approval_revision_mismatch")
    messages = list(state["messages"])
    for index, message in enumerate(messages):
        if (
            isinstance(message, ToolMessage)
            and message.tool_call_id == session.state["awaiting_call"]
        ):
            revised = prepared({"id": message.tool_call_id, "name": message.name}, proposal, [])
            messages[index] = revised.model_copy(update={"id": message.id})
    session.state["proposal"] = proposal
    return {**state, "messages": messages}
