"""A framework approval boundary around the standard Agent subgraph."""

from typing import Annotated, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.types import interrupt

from agent.runtime import memory
from agent.runtime.results import TurnResult, receipt_observations
from agent.runtime.writes import bound_context


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


def review(state, config):
    action, approval = state["pending_action"], state["approval"]
    if action["jti"] != approval["jti"] or approval["scope"] != binding(config):
        raise ValueError("待确认任务与当前身份或会话不一致")
    facts = interrupt({"jti": action["jti"]})
    if facts.get("jti") != action["jti"] or not facts.get("execution", {}).get("receipts"):
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


def graph(saver, agent=None, commit=None):
    builder = StateGraph(ConversationState)
    builder.add_node("agent", agent or _execution_required)
    builder.add_node("approval", review)
    builder.add_node("commit", commit or _commit)
    builder.add_edge(START, "agent")
    boundary = "commit" if commit is not None else "agent"
    if commit is not None:
        builder.add_edge("agent", "commit")
    builder.add_conditional_edges(
        boundary, lambda state: "approval" if state.get("approval") else END)
    builder.add_edge("approval", "agent")
    if commit is None:
        builder.add_edge("commit", END)
    return builder.compile(checkpointer=saver)
