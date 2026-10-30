"""Project completed Agent output into the parent graph's conversation commit."""

from langchain_core.messages import AIMessage, ToolMessage

from agent.runtime import checkpoint_store, memory
from agent.runtime.results import TurnResult, complete_reply
from agent.runtime.turns import check_cancelled


def prepare(session, state):
    result, _, _ = complete_reply(session.outcome, "".join(session.reply_parts))
    previous = state.get("messages") or []
    seen = {message.tool_call_id for message in previous if isinstance(message, ToolMessage)}
    seen.update((session.state.get("context_update") or {}).get("covered_tool_calls", []))
    observations = [message for message in session.observations
                   if not (isinstance(message, ToolMessage) and message.tool_call_id in seen)
                   and not (isinstance(message, AIMessage) and message.tool_calls
                            and all(call["id"] in seen for call in message.tool_calls))]
    task = session.task or {}
    incoming = [] if task.get("input_saved") else task.get("input", session.turn_input)
    return {"reply": result.reply, "execution": result.execution,
            "messages": memory.turn_messages([*incoming, *session.applied_messages],
                                              observations, result.reply, session.principal),
            "context_update": session.state["context_update"]}


def commit(state, config, *, session):
    check_cancelled(config)
    checkpoint_store.require_active(session.store.saver, config["configurable"]["thread_id"])
    turn = state["turn"]
    update = {"messages": memory.checkpoint_messages(
        state.get("messages") or [], turn["messages"], context_update=turn["context_update"]),
        "run": ({**state["run"], "input_saved": True}
                if state.get("approval") and state.get("run") else None),
        "turn": None}
    execution = turn["execution"] or {}
    if "receipts" in execution:
        update["receipts"] = execution["receipts"]
    session.committed_result = TurnResult(turn["reply"], turn["execution"])
    return update
