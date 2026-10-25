"""Application data persisted alongside standard framework messages."""

from langchain.agents.middleware import AgentState

from agent.article_evidence import ArticleEvidence
from agent.runtime.results import TurnResult


class ExecutionState(AgentState, total=False):
    loaded: list[str]
    evidence: dict
    result_chars: int
    proposal: dict | None
    awaiting_call: str | None
    resume: dict | None


def capture(session):
    outcome = session.outcome
    proposal = ({"reply": outcome.reply, "execution": outcome.execution}
                if outcome and (outcome.execution or {}).get("pending_confirm") else None)
    return {"loaded": list(session.loaded), "evidence": session.article_evidence.snapshot(),
            "result_chars": session.result_chars, "proposal": proposal,
            "awaiting_call": session.awaiting_call if proposal else None}


def restore(session, state):
    for domain in state.get("loaded") or []:
        if domain in session.domains and domain not in session.loaded:
            session.tools.update(session.domains[domain])
            session.loaded.append(domain)
    if state.get("evidence"):
        session.article_evidence = ArticleEvidence.restore(state["evidence"])
    session.result_chars = state.get("result_chars", 0)
    session._bind_menu()
    proposal = state.get("proposal")
    if proposal:
        session.outcome = TurnResult(proposal["reply"], proposal["execution"])
        session.awaiting_call = state["awaiting_call"]


def seed(state):
    """Only application fields and standard messages cross the subgraph boundary."""
    return {key: state[key] for key in
            ("messages", "loaded", "evidence", "result_chars", "proposal", "awaiting_call")
            if key in state} | {"resume": None}
