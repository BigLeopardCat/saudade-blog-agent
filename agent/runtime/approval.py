"""A framework approval boundary around the standard Agent subgraph."""

import json
import uuid
from functools import partial
from typing import Annotated, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.types import interrupt

from agent.runtime import memory
from agent.runtime.results import TurnResult, receipt_observations
from agent.runtime.writes import bound_context
from tools.result import ok
from utils.trace import record


class ConversationState(TypedDict, total=False):
    messages: Annotated[list, add_messages]
    pending_confirm: dict | None
    pending_action: dict | None
    approval: dict | None
    approved_result: dict | None
    receipts: list
    run: dict | None
    turn: dict | None


def binding(config):
    principal, cid = bound_context(config)
    return {"uid": principal.uid, "role": principal.known_role, "conversation_id": cid}


def matches(approval, grant, config):
    try:
        return (isinstance(approval, dict) and approval.get("jti") == grant.get("jti")
                and approval.get("scope") == binding(config))
    except ValueError:
        return False


def review(state, config, *, feedback=None):
    action, approval = state["pending_action"], state["approval"]
    if action["jti"] != approval["jti"] or approval["scope"] != binding(config):
        raise ValueError("待确认任务与当前身份或会话不一致")
    facts = interrupt({"jti": action["jti"]})
    if facts.get("jti") != action["jti"]:
        raise ValueError("确认恢复需要当前操作的真实执行回执")
    if facts.get("kind") == "feedback":
        if feedback is None:
            raise ValueError("approval_feedback_not_available")
        execution = feedback(config).execution
        observed = {"status": "superseded", "scope": "operation", "executed": False,
                    "business_state": "not_observed"}
        observations = []
        for spec in action["specs"]:
            observations.extend(memory.observation({"name": spec["tool"], "args": spec["args"],
                "id": "superseded_" + uuid.uuid4().hex}, ok(json.dumps(observed))))
        principal = config["configurable"]["principal"]
        current = memory.turn_messages(facts.get("input") or [], observations, "", principal)[:-1]
        record("native", "approval_feedback", facts=observed)
        return {"pending_confirm": None, "pending_action": None, "approval": None,
                "approved_result": {**facts, "execution": execution},
                "messages": memory.checkpoint_messages(state.get("messages") or [], current)}
    if not facts.get("execution", {}).get("receipts"):
        raise ValueError("确认恢复需要当前操作的真实执行回执")
    result = TurnResult(facts["reply"], facts["execution"])
    principal = config["configurable"]["principal"]
    current = memory.turn_messages(facts.get("input") or [],
                                   receipt_observations(result), result.reply, principal)
    return {"pending_confirm": None, "pending_action": None, "approval": None,
            "approved_result": facts,
            "receipts": facts["execution"]["receipts"],
            "messages": memory.checkpoint_messages(state.get("messages") or [], current)}


def _commit(state):
    return {}


def _execution_required(state):
    raise RuntimeError("Conversation maintenance cannot dispatch Agent execution")


def graph(saver, agent=None, commit=None, *, feedback=None):
    builder = StateGraph(ConversationState)
    builder.add_node("agent", agent or _execution_required)
    builder.add_node("approval", partial(review, feedback=feedback) if feedback else review)
    builder.add_node("commit", commit or _commit)
    builder.add_edge(START, "agent")
    builder.add_edge("agent", "commit")
    builder.add_conditional_edges(
        "commit", lambda state: "approval" if state.get("approval") else END)
    builder.add_edge("approval", "agent")
    return builder.compile(checkpointer=saver)
