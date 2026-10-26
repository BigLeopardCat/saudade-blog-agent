"""Application data persisted alongside standard framework messages."""

from langchain.agents.middleware import AgentState

from agent.runtime.results import TurnResult


class ExecutionState(AgentState, total=False):
    loaded: list[str]
    result_chars: int
    proposal: dict | None
    awaiting_call: str | None
    resume: dict | None
    run_id: str | None
    terminal: dict | None
    effects: dict


def capture(session):
    outcome = session.outcome
    proposal = ({"reply": outcome.reply, "execution": outcome.execution}
                if outcome and (outcome.execution or {}).get("pending_confirm") else None)
    return {"loaded": list(session.loaded),
            "effects": dict(session.effects),
            "run_id": session.task.get("id") if session.task else None,
            "terminal": ({"reply": outcome.reply, "execution": outcome.execution}
                         if outcome and proposal is None else None),
            "result_chars": session.result_chars, "proposal": proposal,
            "awaiting_call": session.awaiting_call if proposal else None}


def restore(session, state):
    session.effects = {**(state.get("effects") or {}), **session.effects}
    if session.effects.get("pending_event"):
        session.outcome_adapter = None
    for domain in state.get("loaded") or []:
        if domain in session.domains and domain not in session.loaded:
            session.tools.update(session.domains[domain])
            session.loaded.append(domain)
    session.result_chars = state.get("result_chars", 0)
    session._bind_menu()
    proposal = state.get("proposal")
    terminal = state.get("terminal")
    if terminal:
        session.outcome = TurnResult(terminal["reply"], terminal["execution"])
    if proposal:
        session.outcome = TurnResult(proposal["reply"], proposal["execution"])
        session.awaiting_call = state["awaiting_call"]


def seed(state):
    """Only application fields and standard messages cross the subgraph boundary."""
    return {key: value for key, value in state.items()
            if not key.startswith("__") and key != "jump_to"} | {"resume": None}
