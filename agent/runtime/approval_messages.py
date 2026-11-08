"""Translate confirmed outcomes into native tool messages without granting success."""

import json

from langchain.agents.middleware import AgentMiddleware, hook_config
from langchain_core.messages import ToolMessage

from agent.runtime.execution_state import restore
from agent.runtime.memory import observation
from agent.runtime.turns import check_cancelled
from tools.result import ok


class ApprovalResumeMiddleware(AgentMiddleware):
    """Consume approval results before the context-capacity boundary."""

    def __init__(self, policy):
        self.policy = policy

    @hook_config(can_jump_to=["tools"])
    def before_model(self, state, runtime):
        if not state.get("resume"):
            return None
        self.policy._recover(state)
        check_cancelled(self.policy.session.config, check_budget=False)
        restore(self.policy.session, state)
        return self.policy._resume(state)


def deferred(call, *, awaiting, available):
    """Describe a scheduling boundary without changing tool access or approval."""
    return ToolMessage(content=json.dumps({
        "status": "not_executed", "reason": "awaiting_prior_confirmation",
        "executed": False, "prepared": False, "target_verified": False,
        "available_in_catalog": available,
        "waiting_for_call_id": awaiting,
        "continuation": "resume_after_prior_approval_and_verified_result",
        "approval": "not_requested_for_this_operation",
    }), name=call["name"], tool_call_id=call["id"], artifact={"phase": "deferred"})


def prepared(call, proposal, todos):
    """Expose preparation and pending work as data, never as execution evidence."""
    visible = {
        "status": "awaiting_confirmation", "executed": False,
        "approval": "required", "approval_scope": "current_operation_only",
        "continuation": "waiting_for_user_decision",
        "tool_execution": "disabled_until_decision",
        "result": proposal["execution"]["pending_confirm"].get("q", proposal["reply"]),
    }
    visible["notices"] = proposal["execution"]["pending_confirm"].get("notices", [])
    facts = proposal["execution"]["pending_confirm"].get("preparation_facts")
    if facts is not None:
        visible["preparation_facts"] = facts
    pending = [item for item in todos if item["status"] != "completed"]
    if pending:
        visible["remaining_work_not_execution_evidence"] = pending
    return ToolMessage(content=json.dumps(visible, ensure_ascii=False),
        name=call["name"], tool_call_id=call["id"], artifact={
            "kind": "prepared", "phase": "awaiting_approval", "proposal": proposal})


def receipt(awaiting, facts):
    unverified = bool(facts["execution"].get("operation_outcome"))
    visible = {"status": "unverified" if unverified else "executed",
               "approval": "user_confirmed",
               "result": facts["reply"], "receipts": [
                   {key: row[key] for key in ("tool", "args", "result", "action") if key in row}
                   for row in facts["execution"]["receipts"]]}
    return awaiting.model_copy(update={"content": json.dumps(visible, ensure_ascii=False),
        "status": "error" if unverified else "success",
        "artifact": {"kind": "unavailable" if unverified else "ok", "data": visible}})


def deferred_results(answered, unverified):
    deferred = [message for message in answered.values()
                if (message.artifact or {}).get("phase") == "deferred"]
    if not unverified:
        return deferred, []
    resolved = [message.model_copy(update={"content": json.dumps({
        "status": "not_executed", "reason": "previous_operation_unverified"}),
        "status": "error", "artifact": None}) for message in deferred]
    answered.update({message.tool_call_id: message for message in resolved})
    return [], resolved


def feedback_observations(messages, facts, awaiting_call):
    """Place the committed revocation fact after the user's latest feedback."""
    observed = facts.get("operation_feedback")
    if not observed:
        return []
    name = next((m.name for m in messages if m.tool_call_id == awaiting_call), "approval_feedback")
    return observation({"name": name, "args": {}, "id": "feedback_" + facts["jti"]},
                       ok(json.dumps(observed, ensure_ascii=False)))
