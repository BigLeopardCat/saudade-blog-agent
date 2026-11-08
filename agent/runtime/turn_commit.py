"""Commit the framework transcript and the application outcome atomically."""

from langgraph.graph.message import add_messages

from agent.runtime import checkpoint_store, memory, transcript
from agent.runtime.results import TurnResult, complete_reply, turn_phase
from agent.runtime.turns import check_cancelled


def prepare(session, state):
    result, _, _ = complete_reply(session.outcome, "".join(session.reply_parts))
    task = session.task or {}
    accepted = [*task.get("input", session.turn_input), *session.turn_input,
                *session.applied_messages]
    messages = (session.framework_state or {}).get("messages")
    if messages is None:
        # An admission failure can precede the first framework model node.
        messages = add_messages(state.get("messages") or [], accepted)
    return {"reply": result.reply, "execution": result.execution,
            "messages": transcript.finish(messages, result.reply, session.principal, accepted),
            "tool_domains": session.state.get("tool_domains")}


def commit(state, config, *, session):
    # Exhausting execution resources cannot prevent committing the known outcome.
    check_cancelled(config, check_budget=False)
    checkpoint_store.require_active(session.store.saver, config["configurable"]["thread_id"])
    turn = state["turn"]
    update = {"messages": memory.checkpoint_messages([], turn["messages"]),
        "run": ({**state["run"], "input_saved": True}
                if state.get("approval") and state.get("run") else None),
        "turn": None, "last_outcome": turn_phase(TurnResult(turn["reply"], turn["execution"])),
        "tool_domains": turn.get("tool_domains")}
    execution = turn["execution"] or {}
    if "receipts" in execution:
        update["receipts"] = execution["receipts"]
    session.committed_result = TurnResult(turn["reply"], turn["execution"])
    return update
